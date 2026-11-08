"""Validate an explicit runtime snapshot from a checkout, never copy .git or data."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

MAX_BYTES = 32 * 1024 * 1024
ROOTS = {'eksmu', 'eksmu_bridge', 'eksmu_receipts', 'verification_lab'}
EXTRA = {'requirements-analysis.txt', 'requirements-anchor.txt', 'requirements-receipts.txt', 'requirements-research.txt', 'scripts/doctor.py'}


def allowed_path(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or '\\' in name:
        raise ValueError('Invalid runtime path')
    p = PurePosixPath(name)
    if p.is_absolute() or any(part in {'..', '.'} or part.startswith('.') for part in p.parts) or p.as_posix() != name:
        raise ValueError('Unsafe runtime path')
    if name not in EXTRA and not (len(p.parts) > 1 and p.parts[0] in ROOTS and (p.suffix in {'.py', '.js', '.css', '.html', '.svg'} or (p.parent.as_posix() == 'eksmu/locales' and p.suffix == '.json') or (p.parent.as_posix() == 'eksmu/static/fonts' and p.suffix in {'.woff2', '.txt'}))):
        raise ValueError('Runtime manifest cannot include credentials or unrelated files')
    return p


def read_checkout(root: Path, manifest: Path) -> dict[str, bytes]:
    raw = manifest.read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError('Manifest too large')
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get('format') != 'eksmu.deploy-source.v1':
        raise ValueError('Unknown source manifest')
    entries = doc.get('files')
    if not isinstance(entries, dict) or not 1 <= len(entries) <= 256:
        raise ValueError('Invalid source manifest entries')
    root = root.resolve(strict=True)
    result: dict[str, bytes] = {}
    total = 0
    for name, expected in entries.items():
        p = allowed_path(name)
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('Invalid source digest')
        target = root
        for part in p.parts:
            target = target / part
            if target.is_symlink():
                raise ValueError('Symlinked runtime source refused')
        if not target.is_file():
            raise ValueError('Missing runtime file: ' + name)
        with target.open('rb') as source:
            body = source.read(MAX_BYTES - total + 1)
        total += len(body)
        if total > MAX_BYTES or hashlib.sha256(body).hexdigest() != expected:
            raise ValueError('Runtime source digest or size mismatch: ' + name)
        result[name] = body
    return result


def stage_checkout(files: dict[str, bytes], destination: Path) -> None:
    # Validate all names and content types before creating the destination.
    paths = [(allowed_path(name), body) for name, body in files.items()]
    if not paths or any(not isinstance(body, bytes) for _, body in paths):
        raise ValueError('Invalid source snapshot')
    if sum(len(body) for _, body in paths) > MAX_BYTES:
        raise ValueError('Source snapshot too large')
    destination.mkdir(mode=0o700)  # Existing installation is never merged or replaced.
    for path, body in paths:
        target = destination.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as output:
            output.write(body)
