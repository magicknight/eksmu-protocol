# 独立执行回执 · receipts v0.1.0

更新：2026-09-20。私有、本地、可选组件；不是社区新版整合包。

## 这次真正实现了什么

`eksmu_receipts` 接收明确版本引用、检查范围和结构化输入，在请求与操作者均明确同意后，运行一个有限的有理数等式检查器，输出 Ed25519 签名回执。接收方独立选择验签公钥，核对请求、版本和输入绑定，并重算结果。不同验证者可以并存；没有全社区指定的唯一可信公钥。

示例 `1/3 + 1/6 = 1/2` 实际按精确分数计算，不是浮点近似或写死的 PASS。仅支持有理数及加、减、乘、除组成的闭表达式，不能推广为验证一般数学定理。未安装的 Lean／AI 检查返回 unsupported，不假装执行。

回执不具有编辑、删帖、排序和资金权限。它可以证明选定签名密钥签署了哪些字节，并可重算这个有限计算；不证明作者、优先权、真实运行机器、运行时间的外部真实性，或输入等式准确表达了原文。

## 安装和最小运行

Python 3.13.5、cryptography 46.0.4 为本次实际测试环境，固定依赖不是“最新版”或安全审计声明。基础发表应用不应依赖这个可选组件。

```bash
python -m pip install -r requirements-receipts.txt
python -m unittest discover -s tests -p 'test_receipts*.py' -v
python -m eksmu_receipts --help
```

下例仅生成本地测试材料。目标 UUID 与摘要是示例占位，不声称对应真实作品。应将临时目录置于仓库外；不要上传私钥、私有输入或私人回执。运行不调用远端服务。

```bash
work=$(mktemp -d)
python -m eksmu_receipts keygen --private "$work/verifier.key" --public "$work/verifier.pub"
python -m eksmu_receipts prepare \
  --input examples/rational-equality.json \
  --revision-id 00000000-0000-4000-8000-000000000001 \
  --revision-sha256 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --scope '示例：只检查给出的有理数等式，不评价论文' \
  --out "$work/request.json" --allow-local-compute
python -m eksmu_receipts run --request "$work/request.json" \
  --private "$work/verifier.key" --out "$work/receipt.json" --allow-local-compute
python -m eksmu_receipts verify --request "$work/request.json" \
  --receipt "$work/receipt.json" --public "$work/verifier.pub"
```

`prepare` 不带同意标志则只创建 record_only；`run` 也必须显式允许本地计算。不能把旧原型的 local_record_only 请求自动升级为执行授权。文件不可覆盖；密钥按本地测试用途保存，尚无密钥恢复、撤销注册表或正式服务密钥管理。

## 边界和状态

检查状态分开：passed、failed、invalid_input、unsupported、resource_limited。意外程序错误使尝试进入 error 并抛出错误，不生成成功回执。普通报告不会自动变成执行回执。

PendingRun 和 ReceiptBook 仅在内存中管理尝试与去重，不是耐久队列。开始前取消保证该对象不执行；开始后取消返回 false，不谎称撤回已经运行的计算。重试创建新 run_id；同一签名者／run_id 的重复回执不重复收录，冲突版本拒绝；其他签名者的结果可并存。

边界限制输入字节数、JSON 深度、表达式节点数和整数位数。不执行上传程序、不支持请求指定文件路径或网络地址；这不是通用沙箱，不足以允许运行任意 Lean 工程。运行进程与签名密钥仍在操作者控制下，不能称为硬件隔离或生产级权限证明。

## 与既有工作的关系

这是 RFC0003 的独立实现进展，使用 `eksmu.receipt-request.v1` 与 `eksmu.execution-receipt.v1`，不冒充已合并 v0.3 A/B/candidate77，也不静默读取它们的 bundle v2。尚未取得那几套实际源码，无法声称已经跑过整合测试。

本轮对现有可用 v0.2 源码重跑 43 项原测试，再进行 5 项独立联测，确认验证不改写数据库、失败不阻止发表、取消不禁止批评与修订、来源标签不能冒充签名回执、迁移不依赖验证器继续运行。新组件另外有 59 项测试。数字按测试对象分别报告，不能与并行原型的69／79／77相加。

完整浏览器 HTTP E2E 再次返回 ERR_BLOCKED_BY_ADMINISTRATOR。未修改限制或换通道绕过，仍标为 BLOCKED。

## 下一步

统一各原型的实际源码与记录契约；在界面中把参与者报告和独立回执分开展示；给目标版本引用增加受控、可审计的导出适配；再设计真正的隔离 Lean 执行器。外部模型、链上广播、奖项申请、真实资金与公开部署仍不启用。

## 技术来源

- Ed25519 API：https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/
- Python Fraction：https://docs.python.org/3/library/fractions.html

本次查阅于2026-09-20；库文档支持算法与接口说明，不支持“本项目已经安全审计”或“数学发现已获认可”等结论。
