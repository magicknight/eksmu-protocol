# Eksmu protocol & independent verifier

[Eksmu](https://eksmu.com) is an open research platform: anyone can publish, without credentials, endorsements, wallets or fees. This repository is the part you should not have to take on trust:

- **[SPEC.md](SPEC.md)** — the public record formats: records, the hash chain, Bitcoin time anchors (OpenTimestamps), publication snapshots, author notices and the public mirror.
- **[eksmu_verify.py](eksmu_verify.py)** — a single-file verifier (Python 3.9+, standard library only, ~250 lines) that copies a site's entire public record and checks it offline.

```bash
python3 eksmu_verify.py pull https://eksmu.com ./eksmu-mirror
python3 eksmu_verify.py verify ./eksmu-mirror
# optional, needs a Bitcoin node:  ots verify -f eksmu-mirror/anchors/1.bin eksmu-mirror/anchors/1.bin.ots
```

## What this guarantees, and what it does not

If the verifier passes and the chain head is covered by a Bitcoin-confirmed anchor, then the operator cannot later edit, delete, reorder or back-date any covered record without every existing mirror detecting it. Keep a mirror and that holds even if the site disappears or changes hands.

It does **not** prove who wrote something (records are not author-signed yet), that anything was first or correct, or that a mirror contains every record (compare heads with an anchor or another mirror). The verifier states these limits in its output.

## Principles

Publishing never depends on an AI score, a formal-proof result, a referee, a degree or a payment. Evaluation, recommendation and moderation are separate from the right to publish, and no single model, majority, credential or founder has final say on what is true. The tools here exist so that these commitments can be checked rather than believed.

## 中文简介

Eksmu 是一个开放科研平台：任何人都能发表，不需要学历、推荐人、钱包或付费。本仓库公开的是不应只靠信任的部分：记录格式规范（SPEC.md）和一个零依赖的单文件核验工具。用它可以把站点的全部公开记录复制下来离线核验。只要链头已被比特币确认的时间锚点覆盖，运营者事后改写、删除、重排或倒填任何已覆盖的记录，都会被所有已有副本发现。它不证明作者身份（目前尚无作者签名），也不证明首创性或研究正确，这些局限会写在核验输出里。

## Status

Early public alpha. Site source code is not yet published; it will follow after a privacy review of its history. Issues and independent implementations of the verifier are welcome.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
