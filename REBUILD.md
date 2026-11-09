# Rebuilding an Eksmu instance

Eksmu is a protocol first and a website second. [`SPEC.md`](SPEC.md) defines the record formats; `eksmu.com` is the **reference instance**, not the only possible one. Priority evidence (the SHA-256 record chain plus Bitcoin-anchored OpenTimestamps proofs) is not tied to any domain, so anyone who keeps a public mirror can keep verifying it, and can stand up a new instance from it.

## Steps (tested 2026-11-09)

```bash
python3 eksmu_verify.py pull https://eksmu.com ./mirror            # download + verify the public mirror
python3 -m eksmu --db ./data/community.sqlite mirror-import ./mirror   # verify again, import into a NEW empty database
python3 -m eksmu --db ./data/community.sqlite verify-chain         # head must equal the source site's head
python3 -m eksmu --db ./data/community.sqlite serve                # http://127.0.0.1:8765
python3 eksmu_verify.py conform http://127.0.0.1:8765              # protocol conformance check
```

On 2026-11-09 this was run against the live site: 10 records were imported into an empty database and the rebuilt chain head was `7122fae94fc75385cafbfc5f5f061e1fc2749c887cc8bf5920e0165c749d32a4`, identical to the live head; the service started. `mirror-import` refuses a mirror that fails verification and refuses a non-empty database.

## What is and is not restored

| Restored | Not restored by `mirror-import` |
|---|---|
| Public records (actors, works, revisions, comments, evidence, attachment commitments) in their original order | Accounts and login credentials (people must register again; the public author identity in the record stays) |
| The record chain, with the same head | Attachment bytes (the commitments stay; fetch bytes from the mirror's publication snapshots or the author) |
| | AI analyses, publication snapshots (still in the mirror directory) |
| | Bitcoin anchor proofs (still in the mirror directory; verify with `ots verify` against your own node) |

## What this is not

- **Not a federation.** Instances do not sync, share a chain or recognise each other. A rebuilt instance starts a new operator history on top of an imported past. Federation is a roadmap idea, not a feature.
- **Not a claim about trust in the new operator.** Records you import can be verified; what the new operator does afterwards (moderation, new records, availability) is theirs to answer for, as with any site.
- **Conformance is format-level.** See SPEC.md §12.

If you run an instance, please keep the governance invariants in [CHARTER.md](CHARTER.md) in mind: publishing never depends on an AI score, a formal-proof result, a referee, a degree, a wallet or a payment.
