# 不依赖浏览器的独立验证进程

更新：2026-09-20。实现入口：`eksmu_bridge.handoff` 与 `eksmu_bridge.worker`。

这是本机文件交接的实验性实现：**登记 → 创建者选择验证公钥并允许交接 → 导出最小任务 → 独立进程计算和签名 → 接口验签、重算并记录**。不是浏览器验收替代品，也不是网络联邦、后台队列或运行任意程序的沙箱。

## 可直接运行的演示

```bash
python -m pip install -r requirements-receipts.txt
python scripts/check_integrated.py
python scripts/demo_file_worker.py
```

演示在临时目录创建测试身份和测试密钥，通过真实本机 HTTP 接口交接任务，调用真实 Python 子进程计算、签名及复核；演示结束关闭本机服务并删除测试数据。没有浏览器、外部网络请求或真实研究成果声明。它验证正确等式、错误等式、取消后的迟到回执，以及继续发表和评论。

## Worker 单独使用

```bash
# 只创建新文件，已有文件不会被覆盖。保管私钥，公开的是命令输出的公钥。
python -m eksmu_bridge.worker keygen --key worker.key

# job.json 必须是你获准接收的本机文件任务。
python -m eksmu_bridge.worker run --job job.json --key worker.key --out result.json --allow-local-compute

# 接收者用另行选定的公钥复核，而不是盲目信任结果自带的公钥。
python -m eksmu_bridge.worker check --job job.json --result result.json --public-key SELECTED_PUBLIC_KEY
```

计算只解释受限有理数表达式，不执行上传程序、命令行或外部模型。Worker进程不接收社区数据库路径、社区身份密钥、网页服务签名密钥或完整原文；任务仍包含用户填写的范围与输入，因此仍可能是私有研究资料，不应自动对外发送。

所有运行都要求已有正确私钥、匹配的预选公钥、有效期内的任务和命令行明确同意。输出存在时不覆盖，避免重跑后丢失已签名回执。服务端记录操作ID，相同操作不会重复新建派单；同一派单重复送回相同结果不会增加记录，冲突结果不能覆盖已记录结果。

## 社区接口

启动现有本机服务：`python -m eksmu serve --with-verification`。保留旧的直接计算路径，以下是新增的文件Worker接口，当前还未增加专门的网页操作面板。

所有POST沿用 `Authorization: Bearer <本地身份密钥>`、`Content-Type: application/json`、`X-Eksmu-Client: local-prototype`。读取派单和任务文件也要求任务创建者身份。密钥不写进URL，不提交到仓库。

| 接口 | 参数或返回 |
|---|---|
| POST `/api/verification/dispatch` | `task_id`, `operation_id`（UUID）, `expected_public_key`（64位hex）, `consent:true`, `ttl_seconds` |
| GET `/api/verification/capsule?dispatch_id=…` | 原任务创建者取最小文件包，不带原文或身份凭证 |
| POST `/api/verification/collect` | `dispatch_id`, `submission`（Worker结果JSON对象） |
| GET `/api/verification/handoffs?task_id=…` | 创建者查看交接与回执状态 |

`task_id` 来自已有prepare接口。任意参与者可以针对别人的文章创建自己的检查任务，但不能使用别人的任务权限。每份派单分别固定验证者公钥；同一个任务可以选择多个不同验证者，不由一个全站名单决定谁有资格贡献证据。该接口的私密读权限用于任务交接，不改变原作品的阅读和讨论规则。

## 接收规则与失败处理

签名有两层：原有执行回执绑定明确版本、输入与计算结果；新增封装还绑定本次派单ID和任务文件摘要，防止把同一回执混用到另一份授权。服务用事先保存的公钥核对，并独立重算有限结果。有效签名但错误计算仍被拒绝。

`ready` 是等待本地文件交接；`recorded` 是已核对并记录，不等于文章正确；`late_cancelled` 和 `late_expired` 保留有效回执但明确它迟于取消或接收期限。取消不可能召回已经复制出去的文件或中止外部进程；禁止承诺这种能力。计算失败与服务故障均不触及发表权限。

每个任务最多8份文件派单，有效期60—86400秒；这些资源规则只影响验证服务。签名不证明授权者真实身份、首次发现或原文语义对应。已有网页直接验证用自己的本机密钥；新Worker使用另行选择的密钥，两者不混用为单一信任来源。

## 实际边界

分进程是应用接口分离，不是操作系统权限隔离。同一系统用户启动的进程仍可能拥有相同文件权限，因此不能声称已经具有不可信代码沙箱。没有网络Worker、自动租约／领取、任意命令执行、真实Lean、AI服务、链上交易或资金控制。

目前接口仍通过原本的可选验证服务开启；旧服务的初始化密钥故障会关闭验证相关接口，但发表继续可用。进一步拆分服务启动、恢复密钥、多进程队列和真正的OS隔离是后续工作。

浏览器端到端按用户决定暂缓且不阻塞本模块开发，详见 ADR0002。
