# RFC 0016：私有本地内容承诺 v1

日期：2026-09-23。状态：已实现的本地数据契约；外部时间证明尚未接入。这是依据用户希望用区块链时间戳支持学术先后讨论而作的工程选择，不代表用户已选定链或外部服务。上位边界见[RFC 0012](0012-publication-timestamps.md)。

## 输入和字节规则

输入必须是通过 `oic.publication-package.v1` 核验的完整材料包。包内的 `oic.publication-snapshot.v1` 已固定作品、正文版本、作者账户标识与化名、论文资料、当时明确选中的附件清单及每份原始字节。使用快照的 `sha256` 字段作为32字节摘要；该摘要由现有 `canonical()` 规则对去掉 `sha256` 的快照 JSON 计算：UTF-8、对象键排序、紧凑分隔符、不转义非ASCII、不允许NaN。附件先按长度和SHA-256核验。文件原始字节不经过PDF重排或文本规范化。

每个证据包生成独立的32字节加密随机数，以64位小写十六进制表示。承诺计算为：

```text
SHA256(UTF8("oic.local-time-evidence.v1") || 0x00 ||
       hex_decode(snapshot.sha256) || hex_decode(nonce_hex))
```

`commitment.sha256`为上述32字节结果的小写十六进制。固定向量：快照摘要为32个`00`字节，随机数为31个`00`字节接`01`，结果是`b7563097754738642aeaaa768e7a99bb1f29a079359477de4eb685332f24f835`。向量只检查编码及算法，不是存证实例。

## 证据包与状态

顶层仅含 `format`、`publication_package`、`commitment`、`external_proof`。`format`固定为`oic.local-time-evidence.v1`，`external_proof`固定为`null`。`commitment`仅含 `scheme`=`sha256-oic-snapshot-nonce-v1`、`snapshot_sha256`、`nonce_hex`、`sha256`。核验器拒绝多余字段、缺件、非规范十六进制、损坏材料、替换的归属声明以及任何伪装的外部证明。重复JSON键也被拒绝。包的文件读取上限为既有发表包90 MiB再加64 KiB；离线核验不访问数据库或网络，也不执行附件。

成功输出只给`status=local_only`和`content_integrity=true`。`platform_recorded_at`是快照内的本站时钟记录；`author_declared_prior_publication`从论文资料逐字带出，可能为空；`public_publication_event`和`external_time_evidence`均为`null`。没有证据包即未启用；不匹配是核验失败。此格式没有待确认或已核验的链状态，不能把`local_only`改称“已上链”。

导出的完整包含正文、附件和随机数，必须作为私有研究资料保管。可单独保留承诺摘要，用`--expect-commitment`检查整包被同时替换的情况；只有证据包自身时，能检查内部一致性，不能独立证明谁在何时持有它。随机数丢失即无法从原快照复算该承诺。后添附件或新正文需要新快照和新承诺，旧包不自动覆盖它们。包内化名与账户标识并非真人身份证明；作者声明、本站时钟和隐私状态也不因计算了承诺而得到外部见证。

本地文件未纳入服务数据库或M1实例备份；作者需自行保留导出包。网页仅展示时间来源及离线用法，不生成或保存本地承诺；没有公开提交接口，现有发表流程完全不依赖此命令。后续外部适配器须有独立格式、隐私审查、确认策略、链核验与备份演练，不能向本格式的`external_proof`填入模拟数据冒充真实结果。
