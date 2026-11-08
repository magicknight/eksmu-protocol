# RFC 0017：外部时间锚定的禁用态准备

日期：2026-09-23。状态：候选评估与禁用态边界；未安装客户端、未连接calendar、未提交承诺、未核验区块。

> 2026-10-08 更新：用户批准记录链与 OpenTimestamps 锚定，已按 [RFC 0029](0029-record-chain-and-anchoring.md) 实现：每日锚定的是全站记录链头，不是单个快照承诺；快照级 `DisabledAnchor` 与 `oic.local-time-evidence.v1` 的 `external_proof=null` 保持不变。用户提出区块链时间戳用途，但未逐项选定链、服务、费用或公开提交。本文件不能用作外发授权。

## 当前软件边界

私有服务中的`DisabledAnchor`只返回`disabled`、空服务/版本/证明和`submission_available=false`；任何调用`submit`都会在本地拒绝。没有启用环境变量、网络客户端、队列或提交HTTP接口。快照API会把该状态与作者自述、本站时钟、公开发表事件缺失分别返回。网页不显示“待确认”或“已核验”状态，因为没有外部证明。`oic.local-time-evidence.v1`继续只允许`external_proof=null`。

## 待评估的公开工具

2026-09-23查看[OpenTimestamps客户端官方发布页](https://github.com/opentimestamps/opentimestamps-client/releases)和[官方使用说明](https://github.com/opentimestamps/opentimestamps-client/blob/master/README.md)。当时发布页列出的最新标签是`opentimestamps-client-v0.7.2`；标签解引用提交为`6d711ab86e00dbb4c5160c2c2b703c551b438856`。这只是后续隔离演练的**候选固定版本**，当前运行环境的`otsclient`未安装，项目依赖未增加该工具。

官方示例中的`ots stamp`会向远端calendar提交摘要，`ots upgrade`可补齐未完成证明，独立链核验需要合适的Bitcoin Core节点。官方还提示提交时间、多个提交的关联和calendar REST交互有隐私影响。因此即使只提交随机化承诺，也属于外部行为，不能在当前private阶段自动执行。[官方说明](https://github.com/opentimestamps/opentimestamps-client/blob/master/README.md)

若以后获得明确授权，拟只把[RFC0016](0016-local-time-evidence.md)承诺的**32字节原值**写成受控输入文件供候选客户端处理；必须记录客户端实际对该文件计算的摘要及证明路径，不能把承诺摘要与OTS文件摘要混称相同。正文、附件、化名、随机数、账户凭证和恢复文件均不交给calendar。`examples/time-anchor-demo/commitment.hex`是公开固定向量，可作为无私人材料的预检输入；它尚未用于`ots stamp`。

接入前还需固定：所选calendar和网络目标、实际发出的请求及可关联元数据、超时和重试、客户端与依赖版本、证明文件大小及解析限制、完整离线链核验方式、确认数/重组后的复检规则、节点数据来源、运行与交易成本上限、失败和升级时的回退。当前这些值均未定；不能因为上游客户端本身可能免费就推断维护或链上成本为零。

版本升级要保存旧证明格式测试向量，在独立环境复算内容承诺→OTS证明→链数据的每一层。模拟证明必须显著标成模拟并不得进入真实“已核验”字段。服务不可用时仍允许自由发表和下载本地快照，不要求钱包、付款或使用单一服务。
