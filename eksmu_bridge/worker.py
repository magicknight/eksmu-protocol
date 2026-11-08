"""Offline file worker. It has no community credentials or network client.

This is trusted local application code, not an OS sandbox for untrusted programs.
It never imports, evaluates, or runs program text from a task.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eksmu_receipts import core as receipts
from .handoff import execute_capsule, verify_submission


def read_document(path: str) -> dict:
    with open(path, "rb") as source:
        return receipts.load(source.read(receipts.MAX_BYTES + 1))


def write_document(path: str, document: dict) -> None:
    raw = receipts.canonical(document) + b"\n"
    with open(path, "xb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    keygen = commands.add_parser("keygen", help="Create a separate worker key; never overwrite")
    keygen.add_argument("--key", required=True)
    run = commands.add_parser("run", help="One local-file dispatch, then exit")
    run.add_argument("--job", required=True)
    run.add_argument("--key", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--allow-local-compute", action="store_true")
    check = commands.add_parser("check", help="Check signature and recompute using your selected key")
    check.add_argument("--job", required=True)
    check.add_argument("--result", required=True)
    check.add_argument("--public-key", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "keygen":
            key = Ed25519PrivateKey.generate()
            with open(args.key, "xb") as output:
                output.write(key.private_bytes_raw())
                output.flush()
                os.fsync(output.fileno())
            print(receipts.public_hex(key))
        elif args.command == "run":
            if not args.allow_local_compute:
                raise ValueError("Explicit --allow-local-compute is required")
            if Path(args.out).exists():
                raise FileExistsError("Output already exists; preserve and resubmit it rather than overwrite")
            raw = Path(args.key).read_bytes()
            if len(raw) != 32:
                raise ValueError("Invalid existing worker key; no key was regenerated")
            result = execute_capsule(read_document(args.job), Ed25519PrivateKey.from_private_bytes(raw), allow_local=True)
            write_document(args.out, result)
            print('Result saved locally. Submit it to the requester; no network request was made.')
        else:
            report = verify_submission(read_document(args.job), read_document(args.result), args.public_key)
            print(receipts.canonical(report).decode("utf-8"))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Rejected: {exc}\n")


if __name__ == "__main__":
    main()
