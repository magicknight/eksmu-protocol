"""Explicit offline CLI. Key paths are operator inputs, never request-controlled."""
import argparse
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from .core import MAX_BYTES, PendingRun, Rejected, canonical, hex_bytes, load, make_request, public_hex, verify


def read_json(path):
    with open(path, "rb") as source:
        return load(source.read(MAX_BYTES + 1))


def write_new(path, raw):
    # Refuse overwrite and pre-existing symlinks. No secret is printed by the CLI.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(raw)


def main():
    parser = argparse.ArgumentParser(description="Offline, optional Eksmu execution receipts")
    subs = parser.add_subparsers(dest="command", required=True)
    keys = subs.add_parser("keygen")
    keys.add_argument("--private", required=True); keys.add_argument("--public", required=True)
    prepare = subs.add_parser("prepare")
    for arg in ("input", "revision-id", "revision-sha256", "scope", "out"):
        prepare.add_argument("--" + arg, required=True)
    prepare.add_argument("--allow-local-compute", action="store_true")
    run = subs.add_parser("run")
    for arg in ("request", "private", "out"):
        run.add_argument("--" + arg, required=True)
    run.add_argument("--allow-local-compute", action="store_true")
    check = subs.add_parser("verify")
    for arg in ("request", "receipt", "public"):
        check.add_argument("--" + arg, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "keygen":
            if Path(args.private).exists() or Path(args.public).exists() or Path(args.private).absolute() == Path(args.public).absolute():
                raise Rejected("Key paths must be different and must not exist")
            key = Ed25519PrivateKey.generate()
            write_new(args.private, key.private_bytes_raw().hex().encode() + b"\n")
            write_new(args.public, public_hex(key).encode() + b"\n")
            print("Local verifier key saved; keep the private file outside source control.")
        elif args.command == "prepare":
            req = make_request(args.revision_id, args.revision_sha256, args.scope, read_json(args.input), allow_local=args.allow_local_compute)
            write_new(args.out, canonical(req) + b"\n")
            print("Request saved; no checker, AI or Lean has run.")
        elif args.command == "run":
            with open(args.private, "rt", encoding="ascii") as source:
                key = Ed25519PrivateKey.from_private_bytes(hex_bytes(source.read(66).strip(), 32))
            receipt = PendingRun(read_json(args.request)).execute(key, allow_local_compute=args.allow_local_compute)
            write_new(args.out, canonical(receipt) + b"\n")
            print("Receipt saved: " + receipt["body"]["result"]["status"])
        else:
            with open(args.public, "rt", encoding="ascii") as source:
                pub = source.read(66).strip()
            result = verify(read_json(args.receipt), read_json(args.request), pub)
            print(canonical(result).decode())
    except (Rejected, OSError, ValueError, UnicodeError) as exc:
        parser.exit(1, "Error: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
