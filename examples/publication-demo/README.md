# 一份可以反复使用的完整演示论文

这套材料用于私有软件验收：从 M01 的有限哥德巴赫检验出发，演示一份论文如何发表、被引用、被另一会话讨论、形成修订，并连同原始材料备份恢复。它不是原创成果申报、Lean 证明或真实用户活动记录。

v1 覆盖 4 至 1000（含端点）的 499 个偶数，v2 扩展至 10000 的 4999 个偶数。筛法生成见证，独立文件中的试除算法逐行检查。两个程序均由本次 AI 工作流程编写；“不同算法”不表示有独立人员审稿。

| 可直接阅读的材料 | v1 | v2 |
|---|---|---|
| 完整正文 | [Markdown](v1/paper.md) | [Markdown](v2/paper.md) |
| 便携阅读稿 | [PDF](v1/paper.pdf) | [PDF](v2/paper.pdf) |
| 排版源文件 | [TeX](v1/paper.tex) | [TeX](v2/paper.tex) |
| 有限见证数据 | [CSV](v1/witnesses.csv) | [CSV](v2/witnesses.csv) |
| 实际计算报告 | [报告](v1/check-report.json) | [报告](v2/check-report.json) |
| 可选论文资料 | [元数据](v1/metadata.json) | [元数据](v2/metadata.json) |

公共文件：[生成器](generate.py)、[试除检查器](check.py)、[验收情境](scenario.json)、[文件摘要清单](manifest.json)。论文、元数据均保留空白许可/未声明许可，不在这里代用户选择项目对外许可证。官方题目仅作背景参考，见论文中的固定版本链接；没有向奖项主办方发送材料。

## 复算

从仓库根目录运行（Python 3、Linux）：

```bash
python examples/publication-demo/reproduce.py
```

它先核对清单，再在干净环境、临时目录、隔离 Python 模式及 CPU/内存/文件/超时上限下执行两份固定的仓库演示程序。检查生成数据与已保存 CSV 逐字节一致、两份报告一致，并故意删行、重复、错序、换入合数及错误和数等，确认检查器拒绝；另检查素性边界。它没有联网逻辑，也不是网络隔离沙箱或任意上传代码的执行器。

只复查某版 CSV，不必导入本站数据库或编译 PDF：

```bash
python examples/publication-demo/check.py examples/publication-demo/v2/witnesses.csv --upper 10000
```

摘要清单检测材料变化，不能单独证明作者、原创性、计算正确性或外部时间。与清单一起被替换的材料，仍需要独立保管的摘要来识别。

## 手动走一遍发表流程

建议使用独立的临时实例保存合成演示，避免混入实际研究。在仓库根目录、应用依赖已经安装的 Python 环境中运行：

```bash
DEMO_DIR=$(mktemp -d)
python -m oic --db "$DEMO_DIR/community.sqlite" serve --port 8766
```

该命令只监听本机。使用现有 SSH 连接时，可另加 `-L 8766:127.0.0.1:8766`，在本地打开 `http://127.0.0.1:8766/`。这是操作说明，本轮自动化不冒充用户电脑上的隧道验收。停止演示用 Ctrl-C；保留目录由使用者自行处理，勿将恢复凭证或数据库提交 Git。

1. 用两个独立浏览器环境创建“演示作者”“演示读者”，各自保存恢复文件。作者选择“计算”，将 v1 正文填入发表表单，并按 v1 元数据填写可选论文资料。
2. 上传 `scenario.json` 中 v1 列出的八个文件，下载正文版本 BibTeX；展开“固定本版发表快照”，勾选八份材料，保存快照链接、摘要和完整包。
3. 读者打开固定链接，下载 PDF、CSV 和代码；从“回到本版讨论与作者声明”进入 v1，发表情境中请求扩至 10000 的评论。资料和评论不赋予读者编辑权限。
4. 作者在 v1 后添一份说明，确认它没有进入旧快照。使用“提交新版本”保存 v2 正文、摘要及修订原因，查看版本差异。旧评论留在 v1；附件不会自动继承，需要重新上传 v2 的八份材料并创建新快照。
5. 分别下载两个快照，按[论文说明](../../docs/guides/PAPERS_README.md)离线核验；选择 v1 时仍能读到旧数据、旧资料和原评论。再按[备份说明](../../docs/guides/BACKUP_README.md)停服备份、恢复到新目录，并从较新权威数据库核对恢复凭证，才重新登录继续讨论。

简单问题可以直接发表，不必采用本例的论文结构或先运行检查器。

## 自动化统一验收

需要安装 Playwright 与其 Chromium。应用 Python 与浏览器工具 Python 可以分开，参数填入本机实际路径：

```bash
python scripts/browser_publication_lifecycle.py \
  --app-python /path/to/application/python \
  --browser /path/to/chromium \
  --report /tmp/publication-lifecycle.json
```

可加 `--app-root /path/to/installed/app`，用已安装代码启动全新的临时数据库；它不会连接或写入运行服务的真实数据库。缺少浏览器动态库时按环境传入 `--library-path`。脚本每次创建新身份和版本 ID，通过实际 UI 完成主要操作，下载后逐文件比较字节，在新目录恢复，重新登录作者、继续讨论并重启。最后删除它自己创建的临时目录；脱敏报告不含恢复凭证或 Cookie。

完整验收覆盖发表、引用、讨论、修订、两个快照、后添附件、离线篡改拒绝、390px 阅读、整库恢复及恢复后继续使用。计算复算与浏览器验收分开，浏览器不会执行上传附件。

## 重排 PDF 与维护材料

已保存的 PDF 可以直接使用。可选重排需要本机 Pandoc、XeLaTeX、ctexart/Fandol 字体和 xurl：

```bash
python examples/publication-demo/render.py
```

渲染器只接收这两份已审阅仓库正文，使用干净环境、临时工作目录、资源上限和关闭 shell escape 的 XeLaTeX；不作为上传文件转换服务。它同时产生 `.tex` 与 `.pdf`，发现缺字或文字溢出即报错。不同工具版本生成的 PDF 不承诺逐字节相同。

修改论文、程序或重新生成 PDF 后，应先检查材料与实际结果相符，再显式更新 `manifest.json` 的 SHA-256，并重跑复算及浏览器验收。清单覆盖情境、代码、版本正文、资料和附件；不自包含清单本身，也不记录临时账户 ID 或机器路径。

这完成 M3 的一份完整研究演示，也提供 M01 的可运行起点。另两份有限演示材料见[M04矩阵构造](../m04-hadamard/README.md)和[M05有理时刻见证](../m05-lonely-runner/README.md)；本包本身只计作 M01 一份。
