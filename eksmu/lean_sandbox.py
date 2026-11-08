"""Run one .lean file in a private Linux namespace with no host home or network."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile


MAX_SOURCE = 256 * 1024
MAX_LOG = 64 * 1024
WALL_SECONDS = 30
DEFAULT_TOOLCHAIN = Path.home() / '.local/share/eksmu-lean-toolchains/lean-4.34.0-linux'


def check_toolchain(path):
    if not path.is_dir() or path.is_symlink():
        raise ValueError('Trusted Lean toolchain directory is missing')
    version = subprocess.run([str(path / 'bin/lean'), '--version'], capture_output=True,
                             text=True, timeout=5, check=True).stdout.strip()
    if not version.startswith('Lean (version 4.34.0,'):
        raise ValueError('Expected pinned Lean 4.34.0')
    return version


def run(source, target, *, toolchain=DEFAULT_TOOLCHAIN):
    source = Path(source)
    if source.suffix != '.lean' or source.is_symlink() or not source.is_file():
        raise ValueError('Expected one regular .lean file')
    with source.open('rb') as stream:
        raw = stream.read(MAX_SOURCE + 1)
    if not raw or len(raw) > MAX_SOURCE:
        raise ValueError('Lean source must be 1 to 262144 bytes')
    raw.decode('utf-8')
    if not isinstance(target, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', target):
        raise ValueError('Expected one ordinary dotted Lean declaration name')
    audit_nonce = secrets.token_hex(16)
    begin = f'EKSMU_AUDIT_BEGIN_{audit_nonce}'.encode()
    end = f'EKSMU_AUDIT_END_{audit_nonce}'.encode()
    checked_source = raw + (f'\n#eval IO.println "{begin.decode()}"\n'
                            f'#check @{target}\n#print axioms {target}\n'
                            f'#eval IO.println "{end.decode()}"\n').encode('utf-8')
    version = check_toolchain(Path(toolchain))
    with tempfile.TemporaryDirectory(prefix='eksmu-lean-isolated-') as directory:
        temp = Path(directory)
        input_file = temp / 'source.lean'
        input_file.write_bytes(checked_source)
        input_file.chmod(0o600)
        root = temp / 'root'
        root.mkdir()
        log = temp / 'output.log'
        with log.open('wb') as output:
            command = ['systemd-run', '--user', '--scope', '--collect', '--quiet',
                       '-p', 'MemoryMax=1G', '-p', 'TasksMax=32', '-p', 'CPUQuota=100%',
                       'unshare', '--user', '--map-root-user', '--mount', '--net', '--pid', '--ipc',
                       '--uts', '--fork', '--kill-child', sys.executable,
                       str(Path(__file__).with_name('lean_namespace_child.py')),
                       str(root), str(Path(toolchain)), str(input_file)]
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True,
                                       env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
                                            'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}',
                                            'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{os.getuid()}/bus'})
            timed_out = False
            try:
                code = process.wait(timeout=WALL_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                code = process.wait(timeout=5)
        setup_completed = (temp / 'sandbox-ready').is_file()
        data = log.read_bytes()[:MAX_LOG]
    audit_completed = begin in data and end in data and data.index(begin) < data.index(end)
    status = ('environment_failed' if not setup_completed else 'timeout' if timed_out
              else 'resource_limited' if code in {-9, -25, 137} else 'lean_process_ok' if code == 0 and audit_completed
              else 'lean_process_failed')
    return {'format': 'eksmu.lean-local-check.v1', 'checked_at': datetime.now(timezone.utc).isoformat(),
            'source_sha256': hashlib.sha256(raw).hexdigest(), 'target': target,
            'audited_input_sha256': hashlib.sha256(checked_source).hexdigest(), 'tool': version,
            'isolation': 'user/mount/pid/network/ipc/uts namespaces; read-only toolchain and system binaries; no host home',
            'limits': {'wall_seconds': WALL_SECONDS, 'cpu_seconds': 20, 'address_space_mib': 8192,
                       'cgroup_memory_mib': 1024, 'cgroup_tasks': 32,
                       'writable_tmpfs_mib': 256, 'log_bytes': MAX_LOG},
            'status': status, 'sandbox_setup_completed': setup_completed,
            'appended_audit_steps_observed': audit_completed,
            'sorry_axiom_observed': b'sorryAx' in data,
            'exit_code': code, 'log_sha256': hashlib.sha256(data).hexdigest(),
            'log': data.decode('utf-8', errors='replace'),
            'semantic_review': 'not_determined', 'official_prize_review': 'not_submitted'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('--target', required=True)
    parser.add_argument('--toolchain', default=str(DEFAULT_TOOLCHAIN))
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.source, args.target, toolchain=args.toolchain), ensure_ascii=False, indent=2))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f'Lean sandbox unavailable: {error}\n')
