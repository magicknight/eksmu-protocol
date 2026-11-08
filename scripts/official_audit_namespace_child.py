"""Trusted setup for the synthetic official audit compatibility check only."""
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys


def mount(*args):
    subprocess.run(['mount', *map(str, args)], check=True, stdout=subprocess.DEVNULL)


def capture(args, cwd, log):
    with Path(log).open('wb') as stream:
        return subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL,
                              stdout=stream, stderr=subprocess.STDOUT, timeout=50).returncode


def main():
    if len(sys.argv) != 7:
        raise SystemExit('expected root, synthetic project, trusted audit, toolchain, output and commit')
    host_home = Path.home()
    root, fixture, audit, toolchain, output = map(Path, sys.argv[1:6])
    commit = sys.argv[6]
    mount('--make-rprivate', '/')
    mount('-t', 'tmpfs', '-o', 'size=512m,nosuid,nodev', 'tmpfs', root)
    for name in ('usr', 'toolchain', 'project', 'audit', 'out', 'proc', 'dev'):
        (root / name).mkdir()
    for name, destination in (('bin', '/usr/bin'), ('lib', '/usr/lib'), ('lib64', '/usr/lib64')):
        (root / name).symlink_to(destination)
    mount('--bind', '/usr', root / 'usr')
    mount('-o', 'remount,bind,ro', root / 'usr')
    mount('--bind', toolchain, root / 'toolchain')
    mount('-o', 'remount,bind,ro', root / 'toolchain')
    shutil.copytree(fixture, root / 'project', dirs_exist_ok=True)
    (root / 'audit/audit.py').touch()
    mount('--bind', audit, root / 'audit/audit.py')
    mount('-o', 'remount,bind,ro', root / 'audit/audit.py')
    mount('--bind', output, root / 'out')
    for name in ('null', 'urandom'):
        (root / 'dev' / name).touch()
        mount('--bind', Path('/dev') / name, root / 'dev' / name)
    mount('-t', 'proc', 'proc', root / 'proc')
    (output / 'sandbox-ready').write_text('namespace prepared\n')
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_AS, (32 * 1024**3, 32 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024**2, 16 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.chroot(root)
    os.chdir('/project')
    os.environ.clear()
    os.environ.update({'HOME': '/project', 'PATH': '/toolchain/bin:/usr/bin',
                       'LANG': 'C', 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1'})
    probe = subprocess.run(['/usr/bin/curl', '--silent', '--max-time', '2',
                            'http://127.0.0.1:8765/api/health'], capture_output=True, timeout=4)
    isolation = {'host_home_visible': host_home.exists(),
                 'host_loopback_probe_exit': probe.returncode,
                 'host_loopback_response_bytes': len(probe.stdout)}
    Path('/out/isolation.json').write_text(json.dumps(isolation), encoding='utf-8')
    if isolation['host_home_visible'] or isolation['host_loopback_probe_exit'] == 0:
        raise SystemExit('host access isolation check failed')
    manifest = {
        'schema_version': 1,
        'project': {'root': '/project', 'repository': 'synthetic local Git fixture',
                    'commit': commit, 'toolchain': 'leanprover/lean4:v4.34.0'},
        'problems': [{'id': 'synthetic', 'source': 'synthetic boolean fixture',
                      'requirements': [{'id': 'main', 'description': 'Both named fixture targets are checked; no prize problem.',
                                        'targets': ['valid', 'sorry'], 'coverage': 'pending'}]}],
        'targets': [{'id': 'valid', 'problem': 'synthetic', 'role': 'theorem',
                     'module': 'AuditFixture', 'source': 'AuditFixture.lean', 'declaration': 'demo_ok'},
                    {'id': 'sorry', 'problem': 'synthetic', 'role': 'theorem',
                     'module': 'AuditFixture', 'source': 'AuditFixture.lean', 'declaration': 'demo_sorry'}]}
    Path('/out/targets.json').write_text(json.dumps(manifest), encoding='utf-8')
    commands = [
        (['/usr/bin/python3', '/audit/audit.py', 'preflight', '/out/targets.json', '--out', '/out/preflight'], 0),
        (['/usr/bin/python3', '/audit/audit.py', 'prepare', '/out/targets.json', '--out', '/out/prepared'], 0),
        (['/toolchain/bin/lake', 'build'], 0),
        (['/usr/bin/python3', '/audit/audit.py', 'run', '/out/targets.json', '--out', '/out/run',
          '--lake', '/toolchain/bin/lake', '--timeout', '30'], 1),
    ]
    results = []
    for index, (argv, expected) in enumerate(commands):
        code = capture(argv, '/project', f'/out/step-{index}.log')
        results.append({'step': index, 'exit_code': code, 'expected_exit_code': expected})
        if code != expected:
            break
    Path('/out/steps.json').write_text(json.dumps(results), encoding='utf-8')
    raise SystemExit(0 if len(results) == len(commands) and all(
        item['exit_code'] == item['expected_exit_code'] for item in results) else 1)


if __name__ == '__main__':
    main()
