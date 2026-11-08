"""Generate bounded DEMO witnesses with a sieve; writes CSV to stdout only."""
import argparse
import csv
from math import isqrt
import sys


def generate(upper):
    if not 4 <= upper <= 10_000 or upper % 2:
        raise ValueError('upper must be even and between 4 and 10000 inclusive')
    prime = bytearray(b'\x01') * (upper + 1)
    prime[0:2] = b'\x00\x00'
    for p in range(2, isqrt(upper) + 1):
        if prime[p]:
            for composite in range(p * p, upper + 1, p):
                prime[composite] = 0
    for n in range(4, upper + 1, 2):
        for p in range(2, n // 2 + 1):
            if prime[p] and prime[n - p]:
                yield n, p, n - p
                break
        else:
            raise ValueError(f'no witness found for {n}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upper', type=int, required=True)
    args = parser.parse_args()
    writer = csv.writer(sys.stdout, lineterminator='\n')
    writer.writerow(('n', 'p', 'q'))
    writer.writerows(generate(args.upper))
