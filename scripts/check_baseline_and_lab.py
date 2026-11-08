"""Run the v0.2 baseline and fixture-lab suites, not other receipt/candidate suites."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ["EKSMU_BASELINE_SOURCE"] = str(ROOT)
suite = unittest.TestSuite()
for pattern in ("test_core.py", "test_http.py"):
    suite.addTests(unittest.TestLoader().discover(str(ROOT / "tests"), pattern=pattern))
suite.addTests(unittest.TestLoader().discover(str(ROOT / "tests_verification")))
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
