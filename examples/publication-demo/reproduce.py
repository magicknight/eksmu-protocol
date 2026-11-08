"""Reproduce checked-in DEMO data and reject deliberately damaged witnesses.

This is a repository fixture check, not a service for executing uploaded code.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def limits():
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main():
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    scenario = json.loads((ROOT / 'scenario.json').read_text())
    checks = []
    with tempfile.TemporaryDirectory(prefix='oic-demo-reproduce-') as directory:
        def run(script, *args):
            return subprocess.run([sys.executable, '-I', str(ROOT / script), *map(str, args)],
                                  cwd=directory, env={'PATH': os.defpath}, capture_output=True,
                                  timeout=15, preexec_fn=limits)
        for version in scenario['versions']:
            folder = ROOT / version['directory']; upper = version['upper_inclusive']
            generated = run('generate.py', '--upper', upper)
            assert generated.returncode == 0, generated.stderr
            assert generated.stdout == (folder / 'witnesses.csv').read_bytes()
            checked = run('check.py', folder / 'witnesses.csv', '--upper', upper)
            assert checked.returncode == 0, checked.stderr
            assert json.loads(checked.stdout) == json.loads((folder / 'check-report.json').read_text())
            checks.append({'version': version['directory'], 'range': [4, upper],
                           'witnesses': upper // 2 - 1, 'reproduced_bytes': True})

        # Keep the number and position of rows valid where possible, so each
        # fault challenges mathematical content as well as the CSV envelope.
        original = (ROOT / 'v1/witnesses.csv').read_text().splitlines()
        faults = {'missing_last': original[:-1], 'duplicate': original[:2] + original[1:],
                  'reordered': original[:1] + [original[2], original[1]] + original[3:],
                  'composite': [s if not s.startswith('10,') else '10,4,6' for s in original],
                  'wrong_sum': [s if not s.startswith('10,') else '10,3,5' for s in original],
                  'one_is_not_prime': [s if not s.startswith('4,') else '4,1,3' for s in original],
                  'wrong_header': ['n,q,p'] + original[1:]}
        for name, lines in faults.items():
            bad = Path(directory) / (name + '.csv'); bad.write_text('\n'.join(lines) + '\n')
            assert run('check.py', bad, '--upper', 1000).returncode != 0, name
        assert run('check.py', ROOT / 'v1/witnesses.csv', '--upper', 1002).returncode != 0
        assert run('generate.py', '--upper', 10001).returncode != 0
    # Exercise square boundaries directly; the checker never imports the sieve.
    spec = importlib.util.spec_from_file_location('demo_check', ROOT / 'check.py')
    checker = importlib.util.module_from_spec(spec); spec.loader.exec_module(checker)
    for n in (0, 1, 4, 9, 25, 49, 121, 169):
        assert not checker.prime_by_division(n), n
    for n in (2, 3, 5, 7, 11, 97):
        assert checker.prime_by_division(n), n
    print(json.dumps({'status': 'PASS', 'demo': True, 'manifest_files': len(manifest['files']),
                      'versions': checks, 'rejected_faults': list(faults) + ['wrong_upper', 'generator_limit'],
                      'primality_edge_cases': 14,
                      'execution': 'fixed repository fixtures, isolated Python mode, clean environment, temporary cwd, CPU/memory/file/time limits; no uploaded-code executor or network isolation claim'},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
