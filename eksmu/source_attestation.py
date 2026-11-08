"""Self-reported source attestation for the running instance (``GET /api/source``).

The server hashes the runtime files listed in the deploy manifest
(``deployment/google-cloud/source-manifest.json``) as they exist on disk, and
publishes the per-file SHA-256 values plus one tree digest. Anyone can then
recompute the same tree digest from a checkout of the public repository with
``eksmu_verify.py verify-source`` or with coreutils::

    sha256sum $(paths) | LC_ALL=C sort -k2 | sha256sum

Limit: this is the operator's own statement. It lets a reader match the
running code to a public commit and detects accidental drift, but a dishonest
operator could serve one tree and report another. See SPEC.md.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

FORMAT = 'eksmu.source-attestation.v1'
REPOSITORY = 'https://github.com/magicknight/eksmu-protocol'
LICENSE = 'AGPL-3.0-or-later'
MANIFEST = 'deployment/google-cloud/source-manifest.json'
ROOT = Path(__file__).resolve().parents[1]

_lock = threading.Lock()
_cache: dict[str, object] = {}


def tree_digest(files: dict[str, str]) -> str:
    """SHA-256 of ``sha256sum``-style lines (``<hex>  <path>\\n``) sorted by path bytes."""
    lines = ''.join(f'{files[p]}  {p}\n' for p in sorted(files, key=lambda s: s.encode('utf-8')))
    return hashlib.sha256(lines.encode('utf-8')).hexdigest()


def compute(root: Path = ROOT) -> dict:
    manifest_path = root / MANIFEST
    base = {'format': FORMAT, 'repository': REPOSITORY, 'license': LICENSE,
            'self_reported': True}
    if not manifest_path.is_file():
        return {**base, 'available': False, 'reason': 'deploy manifest not present in this installation'}
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    listed = manifest.get('files') or {}
    actual: dict[str, str] = {}
    missing: list[str] = []
    for rel in sorted(listed):
        path = (root / rel).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            missing.append(rel)
            continue
        actual[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    mismatched = sorted(rel for rel, sha in actual.items() if listed.get(rel) != sha)
    return {
        **base,
        'available': True,
        'version': manifest.get('version'),
        'manifest_sha256': hashlib.sha256(raw).hexdigest(),
        'tree_sha256': tree_digest(actual),
        'files_total': len(listed),
        'files_matching_manifest': len(actual) - len(mismatched),
        'mismatched': mismatched,
        'missing': missing,
        'files': actual,
        'not_verified': [
            'self-reported by the operator; it cannot prove the process is executing these bytes',
            'covers only the runtime files listed in the deploy manifest, not the OS, Python, nginx or dependencies',
        ],
    }


def attestation() -> dict:
    """Computed once per process: Python modules are loaded at start, so the start-time view is the honest one."""
    with _lock:
        if 'value' not in _cache:
            _cache['value'] = compute()
        return _cache['value']  # type: ignore[return-value]
