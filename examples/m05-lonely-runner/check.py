"""Check exact rational witnesses for three fixed runners; no floating point."""
from fractions import Fraction
import json
from pathlib import Path
import re
import sys


def rational(value):
    if type(value) is not str or not re.fullmatch(r'[0-9]+/[1-9][0-9]*', value):
        raise ValueError('Use a nonnegative rational in numerator/denominator form')
    return Fraction(value)


def circle_distance(value):
    remainder = value % 1
    return min(remainder, 1 - remainder)


def check(document):
    if type(document) is not dict or set(document) != {'speeds', 'witnesses'} or document['speeds'] != [0, 1, 3]:
        raise ValueError('This example fixes speeds 0, 1, 3')
    witnesses = document['witnesses']
    if type(witnesses) is not list or len(witnesses) != 3:
        raise ValueError('One witness per runner is required')
    details = []
    for index, witness in enumerate(witnesses):
        if type(witness) is not dict or set(witness) != {'runner', 'time'} or type(witness['runner']) is not int or witness['runner'] != index:
            raise ValueError('Runner index is missing, repeated or out of order')
        time = rational(witness['time'])
        speed = document['speeds'][index]
        distances = [circle_distance((speed - other) * time)
                     for j, other in enumerate(document['speeds']) if j != index]
        if any(distance < Fraction(1, 3) for distance in distances):
            raise ValueError(f'Runner {index} is too close to another runner')
        details.append({'runner': index, 'time': witness['time'], 'distances': [str(x) for x in distances]})
    return {'status': 'PASS', 'threshold': '1/3', 'method': 'exact rational modular distances', 'checks': details}


if __name__ == '__main__':
    source = Path(sys.argv[1]) if len(sys.argv) == 2 else Path(__file__).with_name('witnesses.json')
    print(json.dumps(check(json.loads(source.read_text(encoding='utf-8'))), ensure_ascii=False))
