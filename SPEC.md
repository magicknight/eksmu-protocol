# Eksmu public record formats — v1

Status: describes what `https://eksmu.com` serves as of 2026-10-29. Since 2026-10-29 every format name uses the `eksmu.` prefix; before that the same formats were named with the historical `oic.` prefix (Open Inquiry Community, the project's working name), and the record-chain domain string was `oic.record-chain.v1`. Apart from the prefix the two generations are identical. A mirror uses one prefix throughout; verifiers should accept either. Changes are additive; a breaking change gets a new format name.

The purpose of these formats is that **anyone can copy the public record of an Eksmu site and check, without trusting the operator, that nothing was silently edited, reordered or back-dated after it was anchored.** They do not encode any judgement about research quality.

## 1. Canonical JSON and digests

`canonical(v)` is JSON with object keys sorted, separators `,` and `:` (no whitespace), UTF-8, non-ASCII characters written literally (not `\u` escaped), and no NaN/Infinity. In Python: `json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)`.

`digest(v)` is the lowercase hex SHA-256 of `canonical(v)` encoded as UTF-8.

An object with a **self digest** has a `sha256` field equal to `digest(object without its sha256 field)`.

## 2. Records

Every public action is an append-only record with a self digest:

```json
{"id": "<uuid>", "type": "actor|exploration|revision|comment|evidence",
 "actor_id": "<uuid>", "created_at": "<ISO-8601 UTC>", "payload": {…}, "sha256": "<hex>"}
```

| type | payload | rules |
|---|---|---|
| `actor` | `alias`, `key_hash` | `actor_id == id`. `key_hash` is an archival fingerprint and is not usable as a login credential. |
| `exploration` | `kind` | A work. Must have at least one revision. |
| `revision` | `exploration_id`, `parent_revision_id`, `title`, `body`, `note`, optional `paper` | Only the work's author may revise. `parent_revision_id` must be the work's previous revision (or `null` for the first). |
| `comment` | `revision_id`, `reply_to`, `body` | Targets an existing revision. |
| `evidence` | `revision_id`, `source`, `tool`, `scope`, `method`, `result`, `limitations`, `status`, `service_verified` | A submitted report about a revision. `service_verified` is always `false`: the site never certifies reports. |

A **record bundle** is `{"format": "eksmu.bundle.v1" | "eksmu.publication-bundle.v1", "records": [...], "sha256": digest(records)}`. Order is storage order and matters for the chain.

`created_at` comes from the server clock. It is only as trustworthy as the operator until the record is covered by an anchor (§4).

## 3. Record chain (`eksmu.record-chain.v1`)

```text
head_0 = 32 zero bytes
head_n = SHA-256( "eksmu.record-chain.v1" || 0x00 || head_(n-1) || record_sha256_n )
```

Heads and record digests are used as raw 32-byte values. Entry *n* is `{"seq": n, "record_id", "record_sha256", "head", "chained_at", "backfilled"}`. `backfilled: true` marks records that existed before the chain was introduced (2026-10-08); their earlier times rest on the server clock alone.

## 4. Anchors

An anchor commits a chain head to Bitcoin via [OpenTimestamps](https://opentimestamps.org/). The stamped file is the 32 raw bytes of `head`. Mirror entry: `{"id", "seq", "head", "method": "opentimestamps", "submitted_at", "state", "bitcoin", "checked", "proof_b64"}`, where `proof_b64` is the base64 `.ots` proof.

`state` and `bitcoin` are **the operator's claims**. To verify an anchor yourself: `ots verify -f <id>.bin <id>.bin.ots` with access to a Bitcoin node. A confirmed anchor proves the head (and therefore every record up to `seq`, in that order with those contents) existed no later than the block time. It does not prove authorship, originality, correctness, or that the content was public at that time.

## 5. Publication snapshots and notices

A **snapshot** (`eksmu.publication-snapshot.v1`, self digest) fixes one revision plus selected attachments: `id`, `created_at`, `previous_snapshot_id`, `publisher {id, alias}`, `exploration` (the full exploration record), `revision` (the full revision record), `materials` (sorted by id: `id, revision_id, actor_id, filename, mime, sha256, created_at, size`).

A **package** (`eksmu.publication-package.v1`) is `{"format", "snapshot", "blobs": [{"sha256", "data_b64"}]}` with exactly one blob per distinct material digest; each blob's bytes must hash to its `sha256` and match the listed `size`.

A **notice** (`eksmu.publication-event.v1`, self digest) is an author statement about a work: `kind` is `note`, `withdraw` or `reinstate`; `previous_event_id` links each notice to the previous one for the same work; `withdraw` and `reinstate` must alternate. Withdrawal is a public statement, not deletion: the record and its critiques remain.

Snapshots and notices are not yet covered by the record chain; they are bound to it only through the full exploration/revision records they embed.

## 6. Public mirror (`eksmu.public-mirror.v1`)

`GET /api/mirror` returns:

```json
{"format": "eksmu.public-mirror.v1", "generated_at", "origin",
 "records": <record bundle>,
 "chain": {"format": "eksmu.record-chain.v1", "entries": [...], "algorithm": {...}},
 "anchors": [...],
 "publications": {"snapshots": [{"id", "exploration_id", "revision_id", "created_at", "sha256"}],
                  "events": [<notice>...], "package_path": "/api/publications/export?snapshot_id={id}"},
 "excluded": [...], "signatures": null}
```

Packages are fetched individually from `package_path`. A mirror directory is laid out as `manifest.json`, `publications/<snapshot id>.json`, `anchors/<id>.bin`, `anchors/<id>.bin.ots`.

**Verification** (implemented by `eksmu_verify.py`):

1. bundle digest and every record's self digest; unique ids; authors exist; only a work's author revises it; revisions in parent order; comments/evidence target existing revisions;
2. recompute the chain from the records in order and compare every head;
3. every anchor's `head` equals the chain head at its `seq`; proof bytes decode and are non-empty;
4. each package: snapshot self digest equals the index digest; embedded exploration and revision are identical to the mirrored records; author consistency; material bytes, digests and sizes; missing packages are reported (mirror not `complete`), not silently ignored;
5. notices: self digests, author is the work's author, per-work history links, withdraw/reinstate alternate.

**What passing does not mean:** records are not author-signed; anchors are not checked against Bitcoin by the verifier; a mirror can only prove what it contains — truncation of the newest records is detectable only by comparing `head` with an anchored head or another independent mirror.

**Excluded from mirrors:** account e-mail addresses, password hashes, sessions and recovery material; private moderation reports, notifications, bookmarks; attachments the author has not pinned into a snapshot; worker queues.

## 7. AI analysis reports and responses

Optional manifest field `analysis: {"reports": [...], "responses": [...]}` (added 2026-10-11; absent in older manifests).

A **report** (`eksmu.analysis-report.v1`, self digest) is produced only when a work's author requests it. Fields: `id`, `revision_id`, `revision_sha256` (must equal the revision record's `sha256`), `created_at`, `analyzer {kind, name (model that served the request), requested_model, prompt_template, operator, cost_borne_by}`, `scope {read, not_checked[]}`, `findings[]`, `limitations`, `suspicious_instructions` (the text contained instructions addressed to the AI), `usage {input_tokens, output_tokens, cost_usd}`.

Version 2 (`eksmu.analysis-report.v2`, from 2026-10-11) adds a referee-report structure, still without any score or accept/reject recommendation: `summary` (neutral restatement of the question, approach and claimed results), `claims[]` (`{"id": "C1"…, "claim", "section", "assessment": "supported"|"partially_supported"|"not_established"|"not_assessed", "reasoning"}` — the model's opinion on whether the manuscript itself establishes each main claim), `strengths[]`, `questions[]` (for the author), `suggestions[]` (steps that would strengthen the work) and `overall` (prose). Version 1 reports remain valid.

Each **finding** is `{"id": "F1"…, "kind": "opinion"|"check", "category", "severity": "blocking"|"major"|"minor"|"note", "confidence", "location": {"section", "quote"}, "statement", "quote_verified"}`. `quote_verified` records whether the cited quote occurs in the revision text (whitespace- and case-insensitive); findings whose quote cannot be found stay visible but are flagged. There is deliberately **no overall score or verdict**, and reports never affect publication or default ordering.

A **response** (`eksmu.analysis-response.v1`, self digest): `report_id`, `finding_id`, `actor_id`, `is_author` (true exactly when the responder is the revision's author), `stance`: `accept|dispute|revised|unclear`, `body`, optional `revision_ref` (a revision of the same work), `created_at`. Anyone signed in may respond.

Verification: report digest; revision digest binding; finding ids in order; responses target an existing finding; `is_author` flag correct.

## 8. Planned
- Author-run analyses (bring your own model key, run locally, upload the report labelled as not reproduced by the site).
- Snapshots and notices folded into the record chain.
- Author signatures (server-held by default, exportable), with old records shown as "claimed at migration", never back-signed.
- Incremental mirror sync by chain sequence.
