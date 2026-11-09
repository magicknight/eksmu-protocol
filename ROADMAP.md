# Eksmu protocol roadmap

Status: a plan, not a promise. Nothing below the "Done" section exists yet, and no dates are given. Each item becomes "done" only when it has a SPEC section, tests, and (for anything involving more than one instance) a real second, independently operated instance exercising it. Until then the word *federation* is not used for what Eksmu does.

Eksmu is a protocol first and a website second: [SPEC.md](SPEC.md) defines the formats, `eksmu.com` is the reference instance, and anyone can verify, mirror or [rebuild](REBUILD.md) it.

## Done (verifiable today)

- Open record formats, a SHA-256 record chain and Bitcoin-anchored OpenTimestamps proofs ([SPEC.md](SPEC.md) §§1-7, 9-10).
- A zero-dependency offline verifier (`eksmu_verify.py`) and a digest-only chain witness (`witness/`).
- Public mirror, running-source attestation (`/api/source`, `verify-source`).
- Rebuild of a new instance from a verified mirror (`mirror-import`; tested 2026-11-09 against the live site, same chain head).
- Instance self-description (`/.well-known/eksmu.json`) and a black-box check (`eksmu_verify.py conform`), [SPEC.md](SPEC.md) §12.

## Planned, in dependency order

1. **Incremental mirror sync.** Pull only the entries after a known chain sequence and detect truncation or a fork by comparing heads; today a mirror is a full download.
2. **Author signatures.** Keys held by authors (exportable), signatures on records. Old records stay labelled "claimed at migration" and are never back-signed. This is what makes an identity portable across instances.
3. **Cross-instance witnessing.** An instance can record another instance's signed chain head, so each chain gains independent witnesses beyond Bitcoin anchors. Witnessing is evidence only; it confers no authority.
4. **Federation (mutual recognition).** Instances publish which other instances they recognise; works can be carried between instances with their provenance. Design rules fixed in advance:
   - Priority between instances is the earliest *verifiable anchored* time, never an operator's say-so.
   - If two instances disagree or the same work is claimed twice, both records are shown; no instance, model, token majority, expert title or founder title decides the outcome.
   - Redactions made for privacy or legal reasons propagate with the record's digest preserved, as in the single-instance case.
   - Recognition is opt-in per instance and revocable; nothing is recognised automatically.
5. **Third-party implementations.** Versioned conformance levels and published test vectors so an implementation written without the Eksmu code can claim a level that `conform` can check.

## Open questions

- Abuse and spam across instances: moderation stays local to each instance; how recognition reacts to a flooded or hostile instance is undecided.
- Key loss and rotation for author signatures without creating a central recovery authority.
- How much of a rebuilt instance's history (accounts, attachment bytes, AI analyses, snapshots) can be carried without exposing private data.
- How a small operator proves independence from the reference instance.

## Non-goals

- No token, coin, staking or "consensus" that decides academic outcomes; publishing never depends on a score, a proof result, a referee, a degree, a wallet or a payment ([CHARTER.md](CHARTER.md)).
- No claim that a conforming or recognised instance is correct, well moderated or trustworthy; conformance and recognition are format-level and evidence-level only.
- No claim that time anchors prove originality or correctness.
