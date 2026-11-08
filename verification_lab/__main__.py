"""Run only a labelled, offline protocol demonstration with synthetic content."""
import argparse
import json
import secrets
import uuid
from pathlib import Path

from .protocol import Lab, Rejected, run_fixture, sha, sign_fixture, timestamp


def example_revision():
    record = {"id": str(uuid.uuid4()), "type": "revision", "actor_id": str(uuid.uuid4()),
              "created_at": timestamp(), "payload": {"exploration_id": str(uuid.uuid4()),
              "parent_revision_id": None, "title": "Synthetic test question",
              "body": "DEMO ONLY. This is not a scientific finding.", "note": ""}}
    record["sha256"] = sha(record)
    return record


def demonstration():
    keys = {"fixture.alpha": secrets.token_bytes(32), "fixture.beta": secrets.token_bytes(32)}
    lab = Lab(":memory:", keys)
    try:
        prepared = lab.prepare(example_revision(), "Exercise the receipt protocol only", "lean_check")
        tid, control = prepared["task"]["id"], prepared["control_key"]
        first = lab.authorize(tid, control, "fixture.alpha")
        second = lab.authorize(tid, control, "fixture.beta")
        run_fixture(lab, first, "fixture.alpha", keys["fixture.alpha"], "check_completed")
        run_fixture(lab, second, "fixture.beta", keys["fixture.beta"], "inconclusive")
        third = lab.authorize(tid, control, "fixture.alpha")
        ticket = lab.begin(third, "fixture.alpha", keys["fixture.alpha"])
        lab.cancel(tid, control)
        lab.finish(sign_fixture(ticket, keys["fixture.alpha"], "resource_limited"))
        result = lab.export()
        assert result["grants"][-1]["acceptance"]["received_after_cancellation"]
        return result
    finally:
        lab.close()


def main():
    parser = argparse.ArgumentParser(description="OFFLINE FIXTURES ONLY; no AI/Lean or public service")
    parser.add_argument("--output", type=Path, help="Create a new audit JSON file; never overwrite")
    args = parser.parse_args()
    try:
        rendered = json.dumps(demonstration(), ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            print(rendered, end="")
        else:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(rendered)
            print("Created a synthetic fixture audit; not a research verification result.")
    except (Rejected, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
