# Eksmu · Open Inquiry Community (开放探索共同体)

> **Before the truth, we are all apprentices. No degree, institution or identity required: anyone can publish their inquiry here and let evidence and discussion speak.**
>
> **在真理面前，我们都是学徒。这里不问学历、机构或身份，任何人都可以发表自己的探索，让证据和讨论说话。**

Live platform: **<https://eksmu.com>** · Verifiable openness & offline checks: **<https://eksmu.com/zh/openness.html>**

This repository contains the complete open-source codebase of the **Eksmu** web platform and backend services, together with the public record protocol specification, zero-dependency offline verification tools, and public Bitcoin-anchored chain witnesses.

---

## 1. Independent Offline Verification (Zero Dependencies)

You do not need to trust the operator of `eksmu.com` to verify the integrity and timestamp proofs of published works and attachments. `eksmu_verify.py` and `proof_bundle.py` require only Python 3.9+ standard library:

```bash
# 1. Pull the full public mirror (records, hash chain, anchors, snapshots, AI analyses) and verify
python3 eksmu_verify.py pull https://eksmu.com ./eksmu-mirror

# 2. Re-run the full cryptographic check completely offline at any time
python3 eksmu_verify.py verify ./eksmu-mirror

# 3. Verify any locally downloaded PDF or LaTeX source archive against the hash chain & Bitcoin anchors
python3 eksmu_verify.py verify-file ./eksmu-mirror ./paper.pdf

# 4. Check that the code eksmu.com reports running matches this checkout (SPEC.md §11)
python3 eksmu_verify.py verify-source https://eksmu.com .

# 5. Audit the standalone chain witness directly from this repository without reaching eksmu.com
python3 proof_bundle.py verify witness/chain-witness.json
# Optional (requires OpenTimestamps client + Bitcoin node):
# ots verify -f witness/anchors/1.bin witness/anchors/1.bin.ots
```

See **[SPEC.md](SPEC.md)** for the complete specification of `eksmu.record-chain.v1`, `eksmu.public-mirror.v2`, `eksmu.record-proof.v1`, `eksmu.chain-witness.v1`, and `eksmu.source-attestation.v1`.

### What this guarantees, and what it does not

- **Tamper & backdating detection**: Once a record or file digest is folded into the SHA-256 record chain and covered by a Bitcoin-confirmed OpenTimestamps anchor, any subsequent modification, deletion, or reordering of covered records is cryptographically detectable.
- **Compliance tombstones (`redactions`)**: If an attachment's raw bytes must be removed for privacy or copyright compliance, its `material` digest remains in the hash chain so historical chain heads and Bitcoin proofs never break.
- **Running code ↔ public source**: `GET /api/source` publishes the SHA-256 of every runtime file the server loaded at start-up; `verify-source` compares it with a checkout of this repository. This is the operator's own statement: it catches drift and pins the exact commit, but cannot by itself prove what the process executes.
- **Explicit limits**: Time anchors prove that a manuscript or attachment byte sequence existed *no later than* the anchored Bitcoin block time. They do **not** prove originality, mathematical/scientific correctness, or real-world author identity.

---

## 2. Run Your Own Eksmu Server Locally

Eksmu is built with Python's standard library + SQLite so that any researcher or community member can run a full local instance or replica node with minimal dependencies:

```bash
python3 -m pip install -r requirements-research.txt
python3 scripts/doctor.py
python3 -m eksmu serve
```

Then open `http://127.0.0.1:8765` in your browser.
- Optional math/code rendering uses local `pandoc` (falls back to clean source view if not installed).
- Optional verification lab worker: `python3 -m eksmu serve --with-verification`.
- Full instance backup & portable blob restore: `python3 -m eksmu --db <path> backup <dest> --portable-blobs --offline`.

---

## 3. Run the Automated Test Suite

The repository includes 680+ Python unit & integration tests, 3 Node.js UI test suites, and deployment/sandbox checks. All tests run locally against temporary directories with zero cloud or network dependencies:

```bash
python3 scripts/check_local.py
```

Or run individual suites:

```bash
python3 scripts/check_integrated.py
node tests_community/test_ui.cjs
node tests_research/test_ui.cjs
node tests_bridge/test_ui.cjs
python3 deployment/google-cloud/test_deployment.py
python3 deployment/google-cloud/test_public_alpha.py
```

---

## 4. Repository Structure

- **[eksmu/](eksmu/)** — Main web platform, HTTP server, SQLite store, SHA-256 record chain (`record_chain.py`, `material_chain.py`), OpenTimestamps anchoring (`chain_anchor.py`, `time_anchor.py`), AI public analysis worker (`analysis.py`, `analysis_worker.py`), LaTeX source inspection (`manuscript.py`), portable backups (`backup.py`), and bilingual frontend (`static/`, `locales/`).
- **[eksmu_bridge/](eksmu_bridge/)**, **[eksmu_receipts/](eksmu_receipts/)**, **[verification_lab/](verification_lab/)** — Verification bridge, signed arithmetic receipts, and isolated verification worker.
- **[deployment/compile/](deployment/compile/)** — Rootless, network-isolated LaTeX compilation container and recorder-file security checks (`compile_entry.py`, `Dockerfile`).
- **[eksmu_verify.py](eksmu_verify.py)**, **[proof_bundle.py](proof_bundle.py)**, **[tools/](tools/)** — Zero-dependency offline mirror, single-file, and proof-bundle verifiers.
- **[SPEC.md](SPEC.md)** & **[witness/](witness/)** — Public cryptographic format specification and Bitcoin-anchored chain witness snapshots.
- **[CHARTER.md](CHARTER.md)** & **[SECURITY.md](SECURITY.md)** — Participation rights, separation of powers, and security policy.
- **[docs/guides/](docs/guides/)** & **[docs/rfcs/](docs/rfcs/)** — User guides and engineering RFCs (`0001`–`0032`).

---

## 5. Governance Invariants

Per **[CHARTER.md](CHARTER.md)**:
1. Publishing never depends on an AI score, a formal-proof result, a referee, an academic degree, institutional affiliation, a crypto wallet, or payment.
2. Evaluation, funding, recommendation, and moderation powers are strictly separated from the right to publish.
3. No single AI model, token majority, credentialed expert, or founder holds final authority over scientific truth.

---

## 6. License

This repository uses a dual-license structure to maximize both **independent verification freedom** and **anti-capture platform openness**:

- **Web Platform, Server, Sandboxes, Scripts, Tests & Docs (`eksmu/`, `eksmu_bridge/`, `eksmu_receipts/`, `verification_lab/`, `deployment/`, `scripts/`, `tests*/`, `integration/`, `examples/`, `docs/`, `CHARTER.md`, `SECURITY.md`)**: Licensed under the **GNU Affero General Public License v3.0 or (at your option) any later version ([LICENSE](LICENSE), `AGPL-3.0-or-later`)**. Anyone may inspect, self-host, fork, or run a community node; any public network service running a modified version must also make its source code available.
- **Protocol Specification, Verifiers & Public Witnesses (`SPEC.md`, `eksmu_verify.py`, `proof_bundle.py`, `tools/`, `witness/`, `tests/test_verify.py`, `tests/fixture-mirror*`)**: Licensed under the **Apache License 2.0 ([LICENSE-APACHE-2.0](LICENSE-APACHE-2.0))**, allowing unrestricted integration into third-party verifiers, archives, and research tooling.
- Bundled web fonts in `eksmu/static/fonts/` are licensed under the SIL Open Font License 1.1 (`LICENSE-chakra-petch.txt`, `LICENSE-jetbrains-mono.txt`).
- Copyright 2026 The Eksmu Authors; see [NOTICE](NOTICE).

## 7. Contributing

Issues and pull requests are welcome. Commits must carry a Developer Certificate of Origin sign-off (`git commit -s`), and contributions are licensed under the license of the files they touch (inbound = outbound). See [CONTRIBUTING.md](CONTRIBUTING.md); report vulnerabilities privately per [SECURITY.md](SECURITY.md).
