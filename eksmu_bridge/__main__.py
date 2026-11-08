"""Independently verify a linked task export using a separately selected key."""
import argparse
import json
from .service import check_export
from eksmu.server import load_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file")
    parser.add_argument("--key", required=True, help="Independently selected Ed25519 public key in hex")
    args = parser.parse_args()
    try:
        with open(args.file, "rb") as source:
            raw = source.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("Task export too large")
        checks = check_export(load_json(raw), args.key)
        print(json.dumps({"checks": checks, "source_semantics_verified": False,
                          "publication_authority": False}, ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Rejected: {exc}\n")


if __name__ == "__main__":
    main()
