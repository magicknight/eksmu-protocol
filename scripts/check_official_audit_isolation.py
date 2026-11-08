"""Run pinned public audit.py on a synthetic Git+Lake fixture in Linux isolation."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent
AUDIT = Path.home() / '.local/share/eksmu-lean-toolchains/official-audit-bcf1866/scripts/audit.py'
TOOLCHAIN = Path.home() / '.local/share/eksmu-lean-toolchains/lean-4.34.0-linux'
EXPECTED_AUDIT_SHA = '5db45dddcb4d588bc27e7c7161fbca5cedaa3e73e1c40843d21f5a214323cd07'


def main():
    if hashlib.sha256(AUDIT.read_bytes()).hexdigest() != EXPECTED_AUDIT_SHA:
        raise ValueError('The pinned public audit.py digest changed')
    tool = subprocess.run([str(TOOLCHAIN / 'bin/lean'), '--version'], check=True,
                          capture_output=True, text=True, timeout=5).stdout.strip()
    if not tool.startswith('Lean (version 4.34.0,'):
        raise ValueError('The pinned Lean toolchain is unavailable')
    with tempfile.TemporaryDirectory(prefix='eksmu-official-audit-synthetic-') as directory:
        base = Path(directory)
        fixture, output, root = [base / item for item in ('fixture', 'out', 'root')]
        fixture.mkdir(); output.mkdir(mode=0o700); root.mkdir()
        (fixture / 'lakefile.toml').write_text('name = "AuditFixture"\nversion = "0.1.0"\n'
                                               'defaultTargets = ["AuditFixture"]\n'
                                               'moreLeanArgs = ["-j", "1"]\n\n'
                                               '[[lean_lib]]\nname = "AuditFixture"\n')
        (fixture / 'lean-toolchain').write_text('leanprover/lean4:v4.34.0\n')
        (fixture / 'lake-manifest.json').write_text('{"version":"1.2.0","packagesDir":".lake/packages",'
                                                    '"packages":[],"name":"AuditFixture","lakeDir":".lake",'
                                                    '"fixedToolchain":false}\n')
        (fixture / 'AuditFixture.lean').write_text('theorem demo_ok : True := by trivial\n'
                                                   'theorem demo_sorry : True := by sorry\n')
        subprocess.run(['git', 'init', '-q', str(fixture)], check=True, timeout=10)
        subprocess.run(['git', '-C', str(fixture), 'add', 'AuditFixture.lean', 'lakefile.toml',
                        'lean-toolchain', 'lake-manifest.json'], check=True, timeout=10)
        subprocess.run(['git', '-C', str(fixture), '-c', 'user.name=Synthetic Fixture',
                        '-c', 'user.email=synthetic@invalid', 'commit', '-qm', 'Synthetic Lean fixture'],
                       check=True, timeout=10)
        commit = subprocess.run(['git', '-C', str(fixture), 'rev-parse', 'HEAD'], check=True,
                                capture_output=True, text=True, timeout=10).stdout.strip()
        command = ['systemd-run', '--user', '--scope', '--collect', '--quiet',
                   '-p', 'MemoryMax=4G', '-p', 'TasksMax=512', '-p', 'CPUQuota=100%',
                   'taskset', '-c', '0', 'unshare', '--user', '--map-root-user', '--mount', '--net', '--pid', '--ipc',
                   '--uts', '--fork', '--kill-child', sys.executable,
                   str(ROOT / 'scripts/official_audit_namespace_child.py'),
                   str(root), str(fixture), str(AUDIT), str(TOOLCHAIN), str(output), commit]
        with (base / 'runner.log').open('wb') as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True,
                                       env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
                                            'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}',
                                            'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{os.getuid()}/bus'})
            try:
                code = process.wait(timeout=180)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                raise RuntimeError('Isolated official audit exceeded total wall limit')
        if code != 0:
            steps = (output / 'steps.json')
            build = (output / 'step-2.log')
            audit_result = (output / 'run/result.json')
            raise RuntimeError('Isolated official audit failed: ' + (base / 'runner.log').read_text()[-2000:] +
                               ' steps=' + (steps.read_text() if steps.exists() else 'not written') +
                               ' build=' + (build.read_text(errors='replace')[-2000:] if build.exists() else 'not run') +
                               ' result=' + (audit_result.read_text(errors='replace')[-5000:] if audit_result.exists() else 'not written'))
        result = json.loads((output / 'run/result.json').read_text())
        preflight = json.loads((output / 'preflight/result.json').read_text())
        prepared = json.loads((output / 'prepared/result.json').read_text())
        isolation = json.loads((output / 'isolation.json').read_text())
        if (result['mechanical_status'] != 'proof_gap' or result['semantic_verdict'] != 'not_determined'
                or [row['status'] for row in result['targets']] != ['standard_axioms_only', 'proof_gap']
                or not result['inputs_stable'] or not preflight['ready_for_target_checks']
                or not prepared['ready_for_target_checks'] or isolation['host_home_visible']
                or isolation['host_loopback_probe_exit'] == 0):
            raise AssertionError('Official audit output did not match the synthetic fixture')
        return {'status': 'PASS', 'date': datetime.now(timezone.utc).date().isoformat(),
                'synthetic_only': True, 'official_public_tool_executed': True,
                'upstream_commit': 'bcf1866ea9a4ae82b32bd95b82ffd812b9ee80d2',
                'audit_script_sha256': EXPECTED_AUDIT_SHA, 'tool': tool,
                'isolation': 'user/mount/net/pid/ipc/uts namespaces, read-only trusted binaries, bounded cgroup and tmpfs',
                'checks': ['official preflight and prepare in isolated fixture',
                           'separate clean Lake build before official run',
                           'valid theorem standard_axioms_only', 'sorry theorem proof_gap',
                           'inputs stable and semantic verdict not_determined',
                           'host home and loopback service inaccessible'],
                'limitations': ['synthetic standard-library Git+Lake project only',
                                'no participant repository or dependency acquisition',
                                'not an official review, submission or award decision']}


if __name__ == '__main__':
    result = main()
    print(json.dumps(result, ensure_ascii=False, indent=2))
