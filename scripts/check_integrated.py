"""Run community, verification, worker and product suites on the same source tree."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ['EKSMU_BASELINE_SOURCE'] = str(ROOT)
suite = unittest.TestSuite()
for directory in ('tests', 'tests_verification', 'integration', 'tests_bridge',
                  'tests_worker', 'tests_community', 'tests_research'):
    suite.addTests(unittest.TestLoader().discover(str(ROOT / directory)))
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
