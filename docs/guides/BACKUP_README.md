# 全实例备份与独立恢复（M1）

此管理工具用于当前 Unix/Linux 私有实例。注意：下面示例中的旧私有 Alpha 2 实例（`open-inquiry-community-alpha2`、`open-inquiry-private.service`）仍运行 2026-10-29 更名前的代码，在该实例上需把命令中的 `-m eksmu` 换成 `-m oic`。它在明确停服窗口内保存整个服务器实例，恢复到**尚不存在的新目录**，不覆盖原实例，不自动启动服务。普通作品导出继续保留，但不能代替实例备份。

## 范围与保存契约

以 `--db /path/data/community.sqlite` 为基准：

| 组件 | 源文件 | 保存内容 |
|---|---|---|
| records（必需） | `community.sqlite` | 作者标识、作品、版本（含M3可选论文资料）、评论、证据；M2恢复凭证与会话；新增私有邮箱、密码哈希、单次邮件代码与可公开的作者资料 |
| publications（可选） | `community.sqlite.publications.sqlite` | 固定发表快照清单、原始附件副本、作者说明与撤回/恢复历史 |
| community | `community.sqlite.community.sqlite` | 主题/协作介绍及修改记录、个人关注收藏、提醒已读状态 |
| research | `community.sqlite.research.sqlite` | 附件原始字节和图片预览、协作任务、参与关系和进展；若已启用，还包括自愿 Lean 报名、撤回与私有检查日志 |
| receipts | `community.sqlite.receipt-tasks.sqlite` | 可选验证任务、尝试及签名回执、已存在的本地 Worker 交接记录 |
| 独立密钥包 | `community.sqlite.receipt-signer.key` | 可选服务签名私钥；必须保存到另一个受保护目录 |

备份目录包含 `manifest.json` 与实际存在的 `records.sqlite`、`community.sqlite`、`research.sqlite`、`receipts.sqlite`、`publications.sqlite`。M3第二项起格式为 `eksmu.instance-backup.v2`，清单记录唯一备份 ID、UTC 创建时间、五个组件的存在状态、逐文件 SHA-256 和大小、应用源文件摘要及 Python/SQLite 版本。创建时间是本机记录，不能当作外部可信时间戳。

新核验器继续读取旧四组件的v1清单；v2必须列出第五组件，即使尚未启用也要记为null。恢复仍要求清单记录的匹配源码，旧快照请配合当时保留的运行文件恢复，不自动升级。单篇发表快照包只保存所选正文/资料/附件，不含全部评论、声明、个人清单或账户状态，不能代替本实例备份。

当前没有历史组件登记表；可选库不存在时记录 `null`，只说明此刻缺失，**不能判断它从未使用还是此前被误删**。主库缺失、孤立 WAL/日志、未知同名前缀组件、未知数据库表或回执库缺少签名密钥时直接拒绝备份。未来增加数据库或改变表结构必须更新范围和测试，不能悄悄漏掉。不要为让检查通过而删掉未知文件。

独立密钥目录只有 `secrets.json` 与 `receipt-signer.key`，通过备份 ID 和摘要绑定数据包。两个目录与源数据目录不能互相包含。新建目录权限为 `0700`、文件为 `0600`；这里没有实现加密，数据包包含私人材料和个人参与记录，必须作为私有备份保管。普通作品导出不包含服务私钥。

以下另行保存：个人恢复凭证或尚未迁移的旧身份密钥、未提交草稿、本地 Worker 自己的私钥及服务外文件、部署配置、依赖环境与匹配的程序源码。服务器保存的摘要不能还原遗失的原始秘密。保留私有 Git 中对应代码，并用清单逐文件摘要核对；恢复命令要求源码一致，Python/SQLite 版本记录供人工核对，不自动下载旧版本或迁移结构。

## 停服后创建备份

先停止所有写入者，包括其他端口上的同实例、旧版本服务、直接操作数据库的脚本和独立 Worker 管理命令。`--offline` 表示操作者已完成此步骤；不是自动停服开关。以下是当前安装的命令示例，日期目录应每次换成新的唯一名称：

```bash
cd "$HOME/.local/share/open-inquiry-community-alpha2/app"
EKSMU_PY="$HOME/.local/share/open-inquiry-community-alpha2/venv/bin/python"
EKSMU_DB="$HOME/.local/share/open-inquiry-community-alpha2/data/community.sqlite"
umask 077
mkdir -p "$HOME/.local/share/open-inquiry-community-backups"
systemctl --user stop open-inquiry-private.service
systemctl --user is-active open-inquiry-private.service
```

确认服务为 `inactive` 后执行。当前未启用验证、没有服务签名密钥的实例：

```bash
"$EKSMU_PY" -m eksmu --db "$EKSMU_DB" backup \
  "$HOME/.local/share/open-inquiry-community-backups/NEW-SNAPSHOT" \
  --offline
```

存在签名密钥时，在同一命令中加 `--secrets-dir /另一个私有位置/NEW-SECRET-SNAPSHOT`；父目录需预先存在，目标目录必须尚不存在。不要省略密钥或生成替代密钥冒充原签名者。成功输出仅含备份 ID、组件状态与检查结果，不输出材料或身份秘密。

备份完成或失败后都要检查结果并恢复原服务。手工操作不要因上一条失败跳过重启：

```bash
systemctl --user start open-inquiry-private.service
systemctl --user is-active open-inquiry-private.service
curl --fail http://127.0.0.1:8765/api/health
```

服务和现有作品导入/导出命令持有共享维护锁，备份持有独占锁，冲突立即报错；锁文件不删除，避免锁失效。所有已有数据库同时持有 SQLite 写入保留锁后，再用 SQLite backup API 生成各库快照，包含已提交但尚未合并进主文件的 WAL 内容。跨库一致性的前提仍是所有写入者已停止；文件锁是合作式维护机制，不是隔离恶意同用户程序的安全边界。技术依据：[Python SQLite backup API](https://docs.python.org/3.11/library/sqlite3.html#sqlite3.Connection.backup)、[SQLite 备份说明](https://www.sqlite.org/backup.html)、[Unix 文件锁](https://docs.python.org/3.11/library/fcntl.html#fcntl.flock)。

## 检查、恢复和演练

### GCS 附件的可携带离线副本

GCS 模式的普通备份只记录对象摘要并检查对象存在，**不复制附件字节**；原桶丢失后不能据此恢复附件。停服窗口中可为新备份加 `--portable-blobs`：

```bash
python -m eksmu --db /私有数据/community.sqlite backup /私有位置/NEW-SNAPSHOT --offline --portable-blobs
python -m eksmu verify-backup /私有位置/NEW-SNAPSHOT
python -m eksmu restore /私有位置/NEW-SNAPSHOT --into /独立私有位置/NEW-RESTORE
```

创建时逐个读取研究附件、预览及发表快照引用的 GCS 字节，核对 SHA-256 并存入各自 SQLite 快照；任一对象缺失或摘要不符时整次备份失败。验证此类备份不需要连接原桶，结果 `independent_blob_bytes_verified=true` 表示字节已包含且核验通过。单个快照数据库仍受 512 MiB 限制；副本必须放在受保护的位置，空间不足时失败，不会标记为成功。

恢复后的演练进程必须取消 `EKSMU_BLOB_BUCKET`（以及避免继承其他生产配置），使用 SQLite 内的字节，且不得连接生产服务或生产桶；检查附件下载和发表快照下载。备份目录与源实例处于同一磁盘不构成异地容灾；应另行选择独立故障域、凭证与定期恢复演练。快照含私人数据且未加密，访问权与保留期限必须控制。旧备份可能包含后来移除的资料，尚无自动删除或恢复时屏蔽机制；恢复前必须人工核对移除记录，不能把旧备份直接上线。

只使用自己保管、来源可信的实例备份。SHA-256 用于检出损坏，不验证作者或备份提供者身份；如果文件与清单同时被替换，摘要不能证明真实性。这不是接受第三方数据库上传的接口。

```bash
"$EKSMU_PY" -m eksmu verify-backup /私有位置/SNAPSHOT
"$EKSMU_PY" -m eksmu restore /私有位置/SNAPSHOT --into /独立私有位置/NEW-RESTORE
```

若清单有签名密钥，检查和恢复均附加 `--secrets-dir /私有位置/SECRET-SNAPSHOT`。只检查数据包时会显示 `secrets_required=true`、`secrets_verified=false`；这种结果不足以进行完整恢复。

恢复会检查文件清单、大小与摘要、SQLite 完整性与外键、受支持表结构、密钥包绑定、源码一致性。当前单个文件上限 512 MiB、清单上限 128 KiB；超限需先评估并调整工具。目标即使为空目录也拒绝覆盖；路径中的软链接亦拒绝。一般失败删除本次新建目录；进程被强制中断时可能留下 `.eksmu-restore-incomplete` 标志，服务拒绝启动该目录，应保留现场并选择另一个新目录重试，不直接移除标志继续服务。

恢复结果数据库固定为 `NEW-RESTORE/community.sqlite`，并保留 `restore-record.json`。在单独端口启动演练实例：

```bash
"$EKSMU_PY" -m eksmu --db /独立私有位置/NEW-RESTORE/community.sqlite serve --port 8766
```

需要检查原有可选回执时显式加 `--with-verification`；它仍只是有限算术工具。恢复不会自动执行待处理任务或外发材料。先确认作品、附件、参与状态和原身份可用，再决定是否切换正式服务；恢复命令本身不改 systemd 配置、不切换入口、不覆盖当前实例。

**M2新增恢复保护：** 恢复核对文件后撤销全部会话，并阻断登录/注册，返回 `account_review_required=true`。主库认证状态有意改变，因此不能再要求恢复后的主库与快照逐字节相同；作品和侧库仍须一致。维护者停服后执行 `reconcile-accounts --from-db /最新权威数据库 --offline`，核对当前恢复凭证，再由用户重新登录。权威源不得仍在运行；缺少较新状态时保持阻断。完整流程与旧M1快照升级说明见[账户管理](ACCOUNTS_README.md)。

邮箱账户加入后，整库备份包含私密邮箱和密码哈希，备份权限与原恢复凭证摘要同等严格。离线账户核对会按版本比较并从较新的权威库恢复邮箱密码与可变公开资料；不复制旧邮件验证/重设代码，所有会话仍失效。不得把旧快照单独启用为可登录实例，或把普通作品包当作账户备份。[新账户契约](../rfcs/0027-email-accounts-public-profiles.md)。

## 复现验收与限制

```bash
python scripts/check_integrated.py
python scripts/demo_backup.py
python deployment/google-cloud/test_deployment.py
```

两身份演练使用临时数据和独立应用进程，覆盖五个数据库、签名私钥、已收回的 Worker 结果、原作者归属、附件字节、原实例重启、恢复实例重启及独立写入。M2脚本增加凭证核对与新会话登录；M3第一项增加论文资料和完整版本绑定；第二项加入两份材料范围不同的快照、引用及作者撤回/恢复声明，比较恢复前后完整内容包及侧库。最初M1演练报告保留为历史记录，当前M3恢复证据与实际私有安装见 PROJECT_STATE。

本机备份不能抵御整台 VM 或磁盘丢失。当前未配置异机副本、定时备份、自动升级回退或灾难恢复服务；后续需要明确保存位置、保留策略及相应授权后实施。若研究库中存在自愿 Lean 任务，其授权记录和私有日志会随**整实例**备份保存，备份目录也必须保持私有；普通作品导出不包含它们。浏览器/跨设备操作、Lean 活动及区块链时间证据不属于当时的 M1 验收。
