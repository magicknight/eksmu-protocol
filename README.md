# Eksmu protocol & independent verifier

[Eksmu](https://eksmu.com) is an open research platform: anyone can publish, without credentials, endorsements, wallets or fees. This repository is the part you should not have to take on trust:

- **[SPEC.md](SPEC.md)** — the public record formats: records, the hash chain, Bitcoin time anchors (OpenTimestamps), publication snapshots, author notices, attachment commitments (`eksmu.public-mirror.v2`) and digest-only chain witnesses (`eksmu.chain-witness.v1`).
- **[eksmu_verify.py](eksmu_verify.py)** — a single-file verifier (Python 3.9+, standard library only) that downloads a site's public mirror and available publication snapshots, checks them offline, and verifies any downloaded PDF or LaTeX source archive against the chain.
- **[proof_bundle.py](proof_bundle.py)** & **[witness/](witness/)** — a digest-only chain witness (`witness/chain-witness.json`) and OpenTimestamps anchor files (`witness/anchors/*.bin`, `*.bin.ots`) so anyone can audit the hash chain and Bitcoin proofs even without reaching `eksmu.com`.

```bash
python3 eksmu_verify.py pull https://eksmu.com ./eksmu-mirror
python3 eksmu_verify.py verify ./eksmu-mirror
python3 eksmu_verify.py verify-file ./eksmu-mirror ./paper.pdf   # or pass a single record-proof JSON
python3 proof_bundle.py verify witness/chain-witness.json
# optional, needs a Bitcoin node:  ots verify -f witness/anchors/1.bin witness/anchors/1.bin.ots
```

## What this guarantees, and what it does not

If the verifier passes and the chain head is covered by a Bitcoin-confirmed anchor, a later rewrite of a covered record can be detected by comparing it with the independently verified anchor or a previously retained mirror that covers the record. A mirror cannot prove that the operator included every record.

It does **not** prove who wrote something (records are not author-signed yet), that anything was first or correct, or that a mirror contains every record (compare heads with an anchor or another mirror). The verifier states these limits in its output.

## Principles

Publishing never depends on an AI score, a formal-proof result, a referee, a degree or a payment. Evaluation, recommendation and moderation are separate from the right to publish, and no single model, majority, credential or founder has final say on what is true. The tools here exist so that these commitments can be checked rather than believed.

## 中文简介

Eksmu 是一个开放科研平台：任何人都能发表，不需要学历、推荐人、钱包或付费。本仓库公开的是不应只靠信任的部分：记录格式规范（SPEC.md）和一个零依赖的单文件核验工具。用它可以下载站点公开镜像及可用的发表快照，离线核验；未固定到快照的附件不包含在内。对比独立核验的已确认锚点或覆盖相关记录的旧镜像，可以发现事后对已覆盖记录的改写、删除或重排。它不证明作者身份（目前尚无作者签名），也不证明首创性或研究正确，这些局限会写在核验输出里。

## Status

Early public alpha. Site source code is not yet published; it will follow after a privacy review of its history. Issues and independent implementations of the verifier are welcome.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
