# RFC 0030：公开镜像与离线核验

日期：2026-10-11。状态：已实现（`oic/mirror.py`，测试 `tests/test_mirror.py`）。依据：ADR 0003 的 O1“可验证的开放”。

## 目的

让任何人不经运营者许可，就能复制本站的全部公开记录，并在离线状态下检查这份副本是否自洽，以及是否与已锚定到比特币的链头一致。这是“我们删不掉、改不了而不被发现”这一承诺的技术基础，比口头的“去中心化”更容易核验。

## 接口

- `GET /api/mirror`：公开清单，格式 `oic.public-mirror.v1`，不接受参数。内容：
  - `records`：与 `/api/export` 相同的公开记录包（作者、作品、版本、评论、分析报告）；
  - `chain`：完整记录链条目（序号、记录 ID、记录摘要、链头、入链时间、是否补链）及算法说明；
  - `anchors`：每个 OpenTimestamps 锚点，含 `.ots` 证明字节（base64）与运营者声称的状态；
  - `publications`：发表快照索引（ID、作品、版本、时间、摘要）与全部作者声明（撤回/恢复/说明）；
  - `excluded`：明确列出不包含的内容；`signatures`：目前为 `null`，即尚无作者签名。
- 快照包仍从 `GET /api/publications/export?snapshot_id=…` 逐个下载，避免一次请求生成过大的响应。

## 命令

```text
python -m oic mirror-pull https://eksmu.com ./eksmu-mirror   # 下载到新目录并核验
python -m oic verify-mirror ./eksmu-mirror                   # 离线重新核验
python -m oic --db <实例库> mirror-export ./out               # 运营者本地直接导出
```

目录结构：`manifest.json`、`publications/<快照ID>.json`、`anchors/<ID>.bin` 与 `<ID>.bin.ots`。后两者可直接用于 `ots verify -f anchors/1.bin anchors/1.bin.ots`（需自备比特币节点）。

## 核验内容

`verify_mirror` 只依赖下载的文件：

1. 记录包摘要、每条记录摘要、作者归属、版本父子顺序与引用（复用 `store.validate`）；
2. 按记录顺序重算整条链，逐条比对链头；
3. 每个锚点必须绑定到链上对应序号的链头，证明字节非空；
4. 每个快照包的内容摘要、材料字节，以及其中的作品与版本必须与镜像记录逐字段相同；缺失的快照包列入 `missing`，并令 `complete=false`；
5. 作者声明按作品形成正确的前后链，撤回与恢复交替出现，声明者为作品作者。

## 结果不代表什么

报告固定输出以下三项为 `false`，界面和宣传不得把镜像核验说成更多：

- `authorship_signed`：记录没有作者签名（见 ADR 0003 的 O4）；
- `external_time_verified`：本工具不连接比特币网络，锚点是否已确认需另行 `ots verify`；
- `completeness_vs_operator_known`：镜像只能证明“给我的这些内容自洽”。运营者若整体截断末尾记录，只有把镜像链头与已锚定链头或其他人的镜像比较才能发现。

## 隐私范围

不包含：账户邮箱、密码哈希、会话与恢复材料；私下行为报告、通知、书签、阅读状态；作者未固定进快照的附件；社区话题元数据与验证队列。作者记录中的 `key_hash` 仍随公开记录导出（`/api/export` 早已如此）。对邮箱账户，它是一枚随机生成后即丢弃的令牌的指纹（`modern_accounts.py` 注明“不是有效恢复凭证”）；对旧式令牌账户，它是 256 位随机令牌的 SHA-256。两种情况都不能据此登录。

## 局限与后续

- 链目前只覆盖主记录库；快照、附件与作者声明有自身摘要与前后链，但未进入锚定链。后续把快照清单与声明纳入记录链（O1 后半）。
- 清单在每次请求时生成。公开记录总量受 `MAX_BUNDLE_BYTES`（16 MiB）限制，当前规模下可以接受；规模增长后改为定时生成的静态文件。
- 没有增量同步；规模增长后按链序号提供分页增量。
