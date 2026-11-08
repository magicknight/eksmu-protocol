# RFC0025：官方公开 Lean 审计脚本的合成项目兼容性检查

状态：2026-09-23，私有工程准备；不接收参与者 Git 仓库，不向奖项系统发送材料。

依据[奖项仓库固定提交的自动化说明](https://github.com/TheJustinSunPrize/awards/blob/bcf1866ea9a4ae82b32bd95b82ffd812b9ee80d2/skills/lean-verify/references/automation.md)，公开 `scripts/audit.py` 先做 Git/Lake 项目的 `preflight`、`prepare`，再要求调用者独立建立隔离、资源限制、可信工具链、依赖来源和干净构建，最后 `run` 才执行目标检查。它输出机械状态，语义判断保持 `not_determined`；脚本自身不是沙箱、独立内核检查器或官方托管 API。

本地检查脚本 `scripts/check_official_audit_isolation.py` 从私有工具目录读取固定 `audit.py`，校验其 SHA-256 `5db45dddcb4d588bc27e7c7161fbca5cedaa3e73e1c40843d21f5a214323cd07`，使用可信 Lean 4.34.0。它创建**合成**且无外部依赖的 Git+Lake 项目，含一个有效 `True` 定理与一个 `sorry` 定理，并写出固定提交的目标清单。它不读取社区实例或用户上传的代码。

检查在 `systemd-run` 资源上限、用户/挂载/网络/PID/IPC/UTS 命名空间、512 MiB 临时根、只读系统与工具链、无宿主家目录的环境中执行。项目副本仅在隔离临时根中可写；先检查宿主家目录和宿主回环服务不可达，再顺序运行公开脚本的 `preflight`、`prepare`、独立干净 `lake build` 与 `run`。合成目标应分别产生 `standard_axioms_only` 和 `proof_gap`，整体为 `proof_gap`，语义结论为 `not_determined`。执行限额是 4 GiB cgroup 内存、512任务、100% CPU配额、32 GiB虚拟地址、120秒 CPU、180秒总墙钟、每条命令50秒及每文件16 MiB；这是本机合成项目兼容性设置，不自动适用于未知投稿。

暂未建立投稿项目的原始 Git 来源/40位提交获取、依赖锁定与可信缓存、逐一人工原题要求映射、未跟踪文件复核、跨版本工具链选择和长期隔离作业管理。作者只上传单个 `.lean` 附件时，不能由它推断完整仓库构建；当前网页的单文件预检继续显示其有限范围。若后续加入项目模式，须在作者针对固定材料另外授权后才读取私有仓库，依赖准备与代码执行分阶段记录；执行失败保留为环境或证据不足，不提升成学术否定。

合成运行记录在脱敏报告。报告里的“官方公开工具已执行”只描述这份固定开源脚本在本机合成项目上的运行，不代表奖项主办方进行了审核、接受投稿或建立合作。
