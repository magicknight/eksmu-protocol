"""Trusted setup process for one isolated Lean file; invoked only by lean_sandbox.py."""
import os
from pathlib import Path
import resource
import subprocess
import sys


def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)


def main():
    if len(sys.argv) != 4:
        raise SystemExit('Expected sandbox root, trusted toolchain and one source file')
    root, toolchain, source = map(Path, sys.argv[1:])
    if not root.is_dir() or not toolchain.is_dir() or not source.is_file():
        raise SystemExit('Sandbox input is missing')
    run('mount', '--make-rprivate', '/')
    run('mount', '-t', 'tmpfs', '-o', 'size=256m,nosuid,nodev', 'tmpfs', str(root))
    for name in ('usr', 'toolchain', 'work', 'proc', 'dev'):
        (root / name).mkdir()
    for name, destination in (('bin', '/usr/bin'), ('lib', '/usr/lib'), ('lib64', '/usr/lib64')):
        (root / name).symlink_to(destination)
    run('mount', '--bind', '/usr', str(root / 'usr'))
    run('mount', '-o', 'remount,bind,ro', str(root / 'usr'))
    run('mount', '--bind', str(toolchain), str(root / 'toolchain'))
    run('mount', '-o', 'remount,bind,ro', str(root / 'toolchain'))
    target = root / 'work/Proof.lean'
    target.touch()
    run('mount', '--bind', str(source), str(target))
    run('mount', '-o', 'remount,bind,ro', str(target))
    (root / 'dev/null').touch()
    run('mount', '--bind', '/dev/null', str(root / 'dev/null'))
    (root / 'dev/urandom').touch()
    run('mount', '--bind', '/dev/urandom', str(root / 'dev/urandom'))
    run('mount', '-t', 'proc', 'proc', str(root / 'proc'))
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_AS, (8 * 1024 * 1024 * 1024, 8 * 1024 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024, 64 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    (source.parent / 'sandbox-ready').write_text('namespaces and mounts prepared\n', encoding='utf-8')
    os.chroot(root)
    os.chdir('/work')
    env = {'HOME': '/work', 'PATH': '/toolchain/bin:/usr/bin', 'LANG': 'C', 'LC_ALL': 'C'}
    os.execve('/usr/bin/setpriv', ['setpriv', '--no-new-privs', '--bounding-set=-all',
                                  '/toolchain/bin/lean', '-j', '1', '/work/Proof.lean'], env)


if __name__ == '__main__':
    main()
