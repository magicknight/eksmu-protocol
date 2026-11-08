# RFC 0029：记录链与每日 OpenTimestamps 锚定

日期：2026-10-08。状态：已实现。用户在 2026-10-08 先后决定“先做第一步记录链，第二步也批准”，即本地记录链与 OpenTimestamps 外部锚定均获授权。只向公共 calendar 发送链头摘要，不发送正文、姓名、邮箱或凭证。

## 解决什么问题

此前每条记录（作品、版本、评论、分析）都有 `sha256`，但记录之间互不关联，`created_at` 来自本站时钟。掌握数据库的人可以改写任一记录并重算摘要，外人无从发现。也就是说，时间只在“信任本站”的前提下成立。

## 第一步：记录链（`oic/record_chain.py`）

每条记录写入时，在同一 SQLite 文件、同一事务中追加一项：

```text
head_0 = 32 个零字节
head_n = SHA256(UTF8("oic.record-chain.v1") || 0x00 || head_(n-1) || record_sha256_n)
```

`record_sha256` 就是记录已有的 `sha256` 字段（去掉该字段后的规范 JSON：键排序、紧凑分隔、UTF-8、不转义非 ASCII）。记录格式、导出包与导入均不变；链存在单独的 `record_chain` 表，导入时在新库重建。

- 旧库首次由新代码打开时，按存储顺序补链，并在条目上标记 `backfilled`。补链条目只说明“补链时已存在”，此前的时间仍只有本站时钟。
- `python -m oic --db … verify-chain` 与实例备份核验（`verify-backup`）重算每条记录摘要、整条链和每个锚点；改写、删除、重排记录或改链都会失败。
- **局限：** 单靠本地链，运营者仍可重建整条链。防篡改来自第二步：链头一旦公开锚定，之后任何改写都与已锚定的链头不符。

## 第二步：每日 OpenTimestamps 锚定（`oic/chain_anchor.py`）

独立的定时任务（不在网站进程内）每 6 小时运行一次 `python -m oic anchor --ots <venv>/bin/ots`：

1. 对待确认的锚点执行 `ots upgrade`，解析证明中的比特币区块头证明（`BitcoinBlockHeaderAttestation`），有则状态改为 `confirmed`。
2. 用 mempool.space 与 blockstream.info 两个公开区块浏览器取该高度区块的 Merkle 根比对，记录结果与区块时间。比对不一致记为错误，绝不显示为已核对。这不是全节点核验，页面如实写明。
3. 若链头自上次锚定后有变化且距上次提交至少 20 小时，把链头的 32 个原始字节写成文件 `head.bin`，执行 `ots stamp`（客户端默认 4 个 calendar，至少 2 个应答；客户端自带随机数），保存 `.ots` 证明，状态 `pending`。

客户端版本固定在 `requirements-anchor.txt`（opentimestamps-client 0.7.2 及其依赖，均已发布两周以上）。网站进程从不导入它；服务不可用时发表不受影响。任务只在写数据库的瞬间持有与网站相同的共享实例锁，因此离线备份（独占锁）不会与之重叠。

## 公开接口与页面

- `GET /api/chain`：链长度、链头、补链数、最近锚点及算法说明。
- `GET /api/chain/entries?from=&limit=`：逐项 `seq`、`record_id`、`record_sha256`、`head`（每页最多 1000 项，只含摘要）。
- `GET /api/chain/proof?record=<UUID>`：作品或版本记录的完整 JSON、前一链头、之后到锚点的记录摘要和锚点信息。参与者记录不提供（其中含凭证指纹）。
- `GET /api/chain/anchor-proof?id=`、`/api/chain/anchor-file?id=`：`.ots` 证明与被锚定的 32 字节文件。
- 作品页新增“时间证据”节：链位置；未锚定、等待确认或“已锚定到比特币区块 #H（区块时间 T）”；是否已与公开区块浏览器核对，以及上述下载链接。

## 第三方如何核验

1. 从记录证明取 `record`，按上述规则重算 `sha256`，应等于记录中的值，且内容与页面一致。
2. 从 `previous_head` 起依次折叠该摘要与 `following_record_sha256`，结果应等于 `anchor.head`。
3. 下载两个文件，运行 `ots verify -f eksmu-chain-N.bin eksmu-chain-N.bin.ots`（需要自己的比特币节点，可为修剪节点）。

结论只是：**该版本内容不晚于该比特币区块形成时已经存在。** 它不证明作者身份、原创性、正确性，也不证明当时已公开。首次发表日期仍来自本站时钟，只是现在有了不依赖本站的时间上限。

## 尚未覆盖

- 发表快照、附件、社区侧数据（话题、关注）和发表事件（撤回/恢复）在其他 SQLite 文件中，不在本链内；快照绑定的正文版本在链内，但附件字节尚未被锚定。下一步可把快照摘要并入同一链或每日锚定的 Merkle 树。
- 独立的离线核验脚本尚未公开发布（仓库为 private）；算法已在接口的 `algorithm` 字段中说明。
- 锚定间隔约一天：一条记录在下一次锚定前只有本站时钟。
