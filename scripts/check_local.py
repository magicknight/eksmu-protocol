"""Run the repository's no-cost checks without GitHub Actions or a cloud VM.

Prerequisites: Python dependencies from requirements-research.txt and Node.js.
All commands run against this checkout, use temporary test data, and save
their output in a new private local temporary directory for diagnosis.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


CHECKS = (
    ("Python integrated", ("scripts/check_integrated.py",)),
    ("Community UI", ("tests_community/test_ui.cjs",)),
    ("Research UI", ("tests_research/test_ui.cjs",)),
    ("Bridge UI", ("tests_bridge/test_ui.cjs",)),
    ("Deployment helpers", ("deployment/google-cloud/test_deployment.py",)),
    ("Public Alpha preparation", ("deployment/google-cloud/test_public_alpha.py",)),
)


def run(repo: Path) -> int:
    node = shutil.which("node")
    if not node:
        print("Node.js is required for the three UI checks; install it and rerun.", file=sys.stderr)
        return 2
    if not (repo / "scripts/check_integrated.py").is_file():
        print(f"Repository checkout not found: {repo}", file=sys.stderr)
        return 2

    logs = Path(tempfile.mkdtemp(prefix="eksmu-local-check-"))
    results: list[tuple[str, int]] = []
    for index, (label, (script,)) in enumerate(CHECKS, start=1):
        command = [node if script.endswith(".cjs") else sys.executable, script]
        logfile = logs / f"{index:02d}-{Path(script).stem}.log"
        print(f"[{index}/{len(CHECKS)}] {label} ...", flush=True)
        with logfile.open("wb") as stream:
            completed = subprocess.run(command, cwd=repo, stdout=stream, stderr=subprocess.STDOUT,
                                       check=False)
        results.append((label, completed.returncode))
        print(f"  {'PASS' if completed.returncode == 0 else 'FAIL'} (exit {completed.returncode})")
        if completed.returncode:
            lines = logfile.read_text(encoding="utf-8", errors="replace").splitlines()
            print("  Last output lines:")
            for line in lines[-18:]:
                print(f"    {line}")

    passed = sum(code == 0 for _, code in results)
    print(f"Result: {passed}/{len(results)} checks passed. Logs: {logs}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1],
                        help="Repository root (default: parent of scripts directory)")
    args = parser.parse_args()
    raise SystemExit(run(args.repo.resolve()))
