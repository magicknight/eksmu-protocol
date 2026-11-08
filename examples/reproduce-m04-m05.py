"""Recheck fixed local demonstration data, including meaningful corruptions."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def module(path):
    spec = importlib.util.spec_from_file_location(path.parent.name + '_' + path.stem, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def rejected(check, document):
    try:
        check(document)
    except ValueError:
        return
    raise AssertionError('Corrupted demonstration data was accepted')


def main():
    matrices = json.loads((ROOT / 'm04-hadamard/matrices.json').read_text(encoding='utf-8'))
    m04 = module(ROOT / 'm04-hadamard/check.py')
    generator = module(ROOT / 'm04-hadamard/generate.py')
    assert matrices == {str(n): rows for n, rows in generator.matrices()}
    m04_result = m04.check(matrices)
    changed = deepcopy(matrices); changed['16'][1][0] *= -1
    rejected(m04.check, changed)
    changed = deepcopy(matrices); changed['8'][0][0] = 0
    rejected(m04.check, changed)
    changed = deepcopy(matrices); changed['4'][0], changed['4'][1] = changed['4'][1], changed['4'][0]
    rejected(m04.check, changed)

    witnesses = json.loads((ROOT / 'm05-lonely-runner/witnesses.json').read_text(encoding='utf-8'))
    m05 = module(ROOT / 'm05-lonely-runner/check.py')
    m05_result = m05.check(witnesses)
    assert [row['distances'] for row in m05_result['checks']] == [
        ['1/2', '1/2'], ['1/3', '1/3'], ['1/2', '1/3']]
    for index, bad in ((0, '0/1'), (1, '1/4'), (2, '1/12'), (0, '0.5'), (0, '1/0')):
        changed = deepcopy(witnesses); changed['witnesses'][index]['time'] = bad
        rejected(m05.check, changed)
    changed = deepcopy(witnesses); changed['witnesses'][1]['runner'] = 0
    rejected(m05.check, changed)
    return {'status': 'PASS', 'scope': 'fixed repository demonstrations only',
            'm04': m04_result, 'm05': m05_result,
            'negative_cases': 9, 'limitations': 'No general conjecture, human independent review or Lean verification'}


if __name__ == '__main__':
    print(json.dumps(main(), ensure_ascii=False, indent=2))
