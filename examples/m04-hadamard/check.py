"""Independently check finite M04 matrices with integer dot products."""
import json
from pathlib import Path
import sys


def check(document):
    if type(document) is not dict or set(document) != {'1', '2', '4', '8', '16'}:
        raise ValueError('Expected exactly the five finite orders')
    for order in (1, 2, 4, 8, 16):
        rows = document[str(order)]
        if type(rows) is not list or len(rows) != order:
            raise ValueError(f'Wrong row count at order {order}')
        for row in rows:
            if type(row) is not list or len(row) != order or any(type(v) is not int or v not in (-1, 1) for v in row):
                raise ValueError(f'Invalid entries at order {order}')
        if rows[0] != [1] * order:
            raise ValueError(f'Wrong first row at order {order}')
        for i, left in enumerate(rows):
            for j, right in enumerate(rows):
                if sum(a * b for a, b in zip(left, right)) != (order if i == j else 0):
                    raise ValueError(f'Nonorthogonal rows at order {order}: {i}, {j}')
        if order > 1:
            half = order // 2
            previous = document[str(half)]
            for i in range(half):
                if rows[i] != previous[i] + previous[i] or rows[i + half] != previous[i] + [-v for v in previous[i]]:
                    raise ValueError(f'Wrong Sylvester recurrence at order {order}')
    return {'status': 'PASS', 'orders': [1, 2, 4, 8, 16], 'method': 'integer row dot products and block recurrence'}


if __name__ == '__main__':
    source = Path(sys.argv[1]) if len(sys.argv) == 2 else Path(__file__).with_name('matrices.json')
    print(json.dumps(check(json.loads(source.read_text(encoding='utf-8'))), ensure_ascii=False))
