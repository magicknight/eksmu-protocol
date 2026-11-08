"""Real Lean 4 checks on fixed synthetic sources; never reads a participant's proof."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time


ROOT = Path(__file__).resolve().parent.parent / 'eksmu'
spec = importlib.util.spec_from_file_location('lean_sandbox', ROOT / 'lean_sandbox.py')
sandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sandbox)


def main():
    with tempfile.TemporaryDirectory(prefix='eksmu-lean-fixtures-') as directory:
        root = Path(directory)
        def check(name, text, target):
            source = root / (name + '.lean')
            source.write_text(text, encoding='utf-8')
            return sandbox.run(source, target)

        valid = check('valid', 'theorem demo_add_zero (n : Nat) : n + 0 = n := by simp\n', 'demo_add_zero')
        assert valid['status'] == 'lean_process_ok' and not valid['sorry_axiom_observed']
        assert "'demo_add_zero' depends on axioms: [propext]" in valid['log']
        sorry = check('sorry', 'theorem demo_sorry : True := by sorry\n', 'demo_sorry')
        assert sorry['status'] == 'lean_process_ok' and sorry['sorry_axiom_observed']
        absent = check('absent', 'theorem demo_other : True := by trivial\n', 'missing_target')
        assert absent['status'] == 'lean_process_failed' and 'Unknown identifier' in absent['log']
        sentinel = root / 'outside-secret.txt'
        sentinel.write_text('SYNTHETIC_SENTINEL_NOT_VISIBLE', encoding='utf-8')
        hidden = check('hidden', 'theorem demo_hidden : True := by trivial\n#eval IO.FS.readFile ' +
                       json.dumps(str(sentinel)) + '\n', 'demo_hidden')
        assert hidden['status'] == 'lean_process_failed'
        assert 'SYNTHETIC_SENTINEL_NOT_VISIBLE' not in hidden['log']
        network = check('network', '''theorem demo_network : True := by trivial
#eval do
  let result ← IO.Process.output { cmd := "/usr/bin/curl", args := #["--silent", "--max-time", "1", "http://127.0.0.1:8765/api/health"] }
  IO.println s!"NETWORK_EXIT:{result.exitCode}"
''', 'demo_network')
        assert network['status'] == 'lean_process_ok' and 'NETWORK_EXIT:7' in network['log']
        previous = sandbox.WALL_SECONDS
        sandbox.WALL_SECONDS = 2
        try:
            start = time.monotonic()
            stopped = check('slow', 'theorem demo_sleep : True := by trivial\n#eval do\n  IO.sleep 10000\n', 'demo_sleep')
            assert stopped['status'] == 'timeout' and time.monotonic() - start < 5
        finally:
            sandbox.WALL_SECONDS = previous
        try:
            check('bad_target', 'theorem demo_ok : True := by trivial\n', 'demo_ok;#eval IO.println "bad"')
        except ValueError:
            pass
        else:
            raise AssertionError('Unsafe target identifier was accepted')
        oversized = root / 'oversized.lean'
        oversized.write_bytes(b' ' * (sandbox.MAX_SOURCE + 1))
        try:
            sandbox.run(oversized, 'demo_ok')
        except ValueError:
            pass
        else:
            raise AssertionError('Oversized source was accepted')
    return {'status': 'PASS', 'synthetic_only': True, 'actual_lean_executed': True,
            'tool': valid['tool'], 'release_archive_sha256':
            'caaa98356098c85dc0fcbbd28e1ec66f39eb6551829972b752ff20e1286b646b',
            'checks': ['valid target and axiom output', 'sorryAx observed despite Lean exit zero',
                       'missing target rejected', 'host file content inaccessible',
                       'host loopback service inaccessible from network namespace',
                       'wall timeout kills long task', 'unsafe target and oversized source rejected'],
            'limitations': ['single Lean file and its imports only; no Lake project or official prize audit',
                            'semantic correspondence and independence not determined',
                            'cgroup and namespaces verified on this VM only']}


if __name__ == '__main__':
    print(json.dumps(main(), ensure_ascii=False, indent=2))
