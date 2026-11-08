"""Generate the finite Sylvester matrices used by the M04 exploration card."""
import json
from pathlib import Path


def matrices():
    matrix = [[1]]
    for order in (1, 2, 4, 8, 16):
        yield order, matrix
        matrix = [[*row, *row] for row in matrix] + [
            [*row, *(-value for value in row)] for row in matrix
        ]


if __name__ == '__main__':
    destination = Path(__file__).with_name('matrices.json')
    destination.write_text(json.dumps({str(n): rows for n, rows in matrices()},
                                      ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(destination)
