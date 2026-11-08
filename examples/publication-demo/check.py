"""Check DEMO CSV using trial division, without importing the generator."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path


def prime_by_division(n):
    if n < 2:
        return False
    divisor = 2
    while divisor * divisor <= n:
        if n % divisor == 0:
            return False
        divisor += 1
    return True


def check(raw, upper):
    if type(upper) is not int or not 4 <= upper <= 10_000 or upper % 2:
        raise ValueError('invalid inclusive upper bound')
    if len(raw) > 256 * 1024:
        raise ValueError('demo CSV exceeds 256 KiB')
    rows = list(csv.reader(io.StringIO(raw.decode('ascii')), strict=True))
    if not rows or rows[0] != ['n', 'p', 'q']:
        raise ValueError('expected n,p,q header')
    if len(rows) - 1 != upper // 2 - 1:
        raise ValueError('missing or extra witness rows')
    for expected_n, row in zip(range(4, upper + 1, 2), rows[1:]):
        if len(row) != 3 or any(not x.isdigit() or len(x) > 5 for x in row):
            raise ValueError('expected three bounded unsigned integers')
        n, p, q = map(int, row)
        if n != expected_n:
            raise ValueError('missing, duplicate or reordered n')
        if not 2 <= p <= q <= n or p + q != n:
            raise ValueError(f'invalid witness bounds or sum at {n}')
        if not prime_by_division(p) or not prime_by_division(q):
            raise ValueError(f'composite witness at {n}')
    return {'format': 'oic.demo-finite-check.v1', 'demo': True,
            'lower_inclusive': 4, 'upper_inclusive': upper,
            'checked_even_integers': upper // 2 - 1,
            'method': 'integer trial division for each supplied p and q; exact ordered coverage',
            'csv_sha256': hashlib.sha256(raw).hexdigest(),
            'finite_witness_check': 'PASS', 'general_conjecture_proved': False,
            'lean_checked': False, 'independent_human_review': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv', type=Path)
    parser.add_argument('--upper', type=int, required=True)
    args = parser.parse_args()
    with args.csv.open('rb') as stream:
        raw = stream.read(256 * 1024 + 1)
    try:
        result = check(raw, args.upper)
    except (ValueError, UnicodeError, csv.Error) as exc:
        parser.exit(1, f'Finite DEMO check failed: {exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
