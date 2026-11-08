# Eksmu 隔离 LaTeX 编译容器（Cloud Run Job）部署指南

本目录提供用于隔离编译作者上传的 LaTeX 源码包（`.zip`、`.tar.gz`、`.tgz`、`.tar`）的容器镜像定义与入口脚本：

- `Dockerfile`：基于官方固定 digest 镜像 `docker.io/texlive/texlive:latest@sha256:540d736c...`，安装 `python3`、`fontconfig`、`ghostscript`、`poppler-utils`、`imagemagick`、`latexml`，写入最小权限 `ImageMagick` 安全策略（仅启用 `PDF`/`PNG`/`SVG` coder 与 Ghostscript PDF/PS 委托，禁用外部 URL、间接读取 `@*` 与 `/etc`、`/proc`、`/sys`、`/home`、`/var` 等路径，固定 `thread=1`），在构建阶段预建系统级 `fontconfig` 缓存（`/var/cache/fontconfig`）与 `luaotfload`/`luatexja` 缓存模板（`/var/cache/eksmu-texmfvar`），并以非 root 用户（UID/GID `10001:10001`）运行。
- `compile_entry.py`：仅依赖 Python 标准库，支持 Cloud Run Job 模式与 `--local IN_FILE OUT_DIR` 离线本地测试模式；编译时将 `SOURCE_DATE_EPOCH` 固定为版本（revision）提交时间戳并设 `FORCE_SOURCE_DATE=1`、`TZ=UTC`（对非负整数且不晚于当前时间的 `SOURCE_DATE_EPOCH` 生效，非法值忽略并记入日志），使 `pdflatex`、`xelatex`（`xdvipdfmx`）与 `lualatex` 生成固定 `CreationDate`、`/ID` 与 `\today` 的可复现 PDF；在完成 PDF 编译后，于独立源码副本目录中运行受 `EksmuLaTeXMLSandbox` 路径沙箱保护的 `latexml` + `latexmlpost` 生成内联 CSS 与 `data:image/*` 图片的自包含单文件 HTML5 + MathML（整个 HTML 阶段含 `latexml`、`latexmlpost`、PDF 插图转 PNG 内联与超出 16 MiB 时的分级降采样共享同一 300 秒截止时间，到点停止后续图片转换并保留 `alt` 文本占位），记录 `latexmlpost` 退出码与工作目录相对文件列表（剥离容器内部临时路径），对已完成 HTML 写入但退出阶段非零退出的可恢复情形予以保留并在日志注明，最终经容器与网站双端 `html.parser` 清理。

> **产品不变量**：发表不依赖编译成功；源码包原样保存在正式附件桶；编译出的 PDF 仅作为附带产物标注“本站从源码编译 · TeX Live <年份> · <引擎>”；编译失败仅向作者展示日志，不影响作品页其他内容与作者自行上传的 PDF。

---

## 1. 按 Digest 固定基础镜像并在 Cloud Build 构建推送

**注意**：本机磁盘空间紧张，**不要在本机执行 `docker build` 或拉取数 GB 的 TeX Live 镜像**，请直接在 Google Cloud Build 中远程构建并推送到 Artifact Registry。

### 1.1 查询并固定 `texlive/texlive:TL2026-historic` 的 Digest

在维护者终端执行：

```bash
gcloud container images describe docker.io/texlive/texlive:TL2026-historic \
  --format='value(image_summary.digest)'
```

（或使用 `docker buildx imagetools inspect docker.io/texlive/texlive:TL2026-historic` 获取 `sha256:...`。）
将 `deployment/compile/Dockerfile` 第一行替换为带 digest 的形式：

```dockerfile
FROM docker.io/texlive/texlive:TL2026-historic@sha256:<查到的64位十六进制digest>
```

### 1.2 创建 Artifact Registry 仓库并用 Cloud Build 构建

```bash
PROJECT_ID="your-gcp-project-id"
REGION="us-central1"
AR_REPO="eksmu-compile"
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/texlive-worker:tl2026-v1"

gcloud artifacts repositories create "${AR_REPO}" \
  --project="${PROJECT_ID}" \
  --repository-format=docker \
  --location="${REGION}" \
  --description="Eksmu isolated LaTeX compile container images"

gcloud builds submit ./deployment/compile \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --tag="${IMAGE_URI}"
```

---

## 2. 创建专用编译存储桶（与正式附件桶完全隔离）

编译容器**绝不**访问正式附件桶（`EKSMU_ATTACHMENT_BUCKET`）。专用编译桶仅暂存待编译源码包 `in/<job_id>.src` 与编译结果 `out/<job_id>/*`，并通过生命周期策略在 7 天后自动清理。

```bash
COMPILE_BUCKET="${PROJECT_ID}-eksmu-compile-staging"

gcloud storage buckets create "gs://${COMPILE_BUCKET}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --uniform-bucket-level-access \
  --public-access-prevention

cat > /tmp/compile-bucket-lifecycle.json <<'EOF'
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {
        "age": 7,
        "matchesPrefix": ["in/", "out/"]
      }
    }
  ]
}
EOF

gcloud storage buckets update "gs://${COMPILE_BUCKET}" \
  --lifecycle-file=/tmp/compile-bucket-lifecycle.json
```

---

## 3. 创建编译专用服务账号（最小权限）

编译容器运行在独立的 `eksmu-tex-compile-runner` 服务账号下，**仅授予专用编译桶的 `objectViewer` 与 `objectCreator`**，不授予正式附件桶、数据库、虚拟机或任何其他云资源的权限：

```bash
COMPILE_SA_NAME="eksmu-tex-compile-runner"
COMPILE_SA_EMAIL="${COMPILE_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud iam service-accounts create "${COMPILE_SA_NAME}" \
  --project="${PROJECT_ID}" \
  --display-name="Eksmu LaTeX Compile Cloud Run Job Runner"

gcloud storage buckets add-iam-policy-binding "gs://${COMPILE_BUCKET}" \
  --member="serviceAccount:${COMPILE_SA_EMAIL}" \
  --role="roles/storage.objectViewer"

gcloud storage buckets add-iam-policy-binding "gs://${COMPILE_BUCKET}" \
  --member="serviceAccount:${COMPILE_SA_EMAIL}" \
  --role="roles/storage.objectCreator"
```

---

## 4. 创建 Cloud Run Job

使用编译专用服务账号创建 Cloud Run Job（任务超时 1800 秒、重试 0 次、2 vCPU、4 GiB 内存、并发度 1）：

```bash
JOB_NAME="eksmu-latex-compile"

gcloud run jobs create "${JOB_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --image="${IMAGE_URI}" \
  --service-account="${COMPILE_SA_EMAIL}" \
  --task-timeout=1800s \
  --max-retries=0 \
  --cpu=2 \
  --memory=4Gi \
  --parallelism=1 \
  --tasks=1 \
  --set-env-vars="COMPILE_BUCKET=${COMPILE_BUCKET}"
```

---

## 5. 授予网站虚拟机服务账号最小 IAM 权限

网站虚拟机服务账号（假设为 `VM_SA_EMAIL`）需要：
1. 对编译桶 `gs://${COMPILE_BUCKET}` 的 `roles/storage.objectCreator` 与 `roles/storage.objectViewer`（上传 `in/<job_id>.src`，读取 `out/<job_id>/result.json`、`compile.log` 与 `<main>.pdf`）；
2. 对该 Cloud Run Job 的 `roles/run.jobsExecutorWithOverrides`（包含 `run.jobs.runWithOverrides` 权限，用于通过 Cloud Run Admin API v2 传入 `JOB_ID`、`MAIN_FILE` 等环境变量覆盖项）以及查看 execution 状态的只读权限（`roles/run.viewer`）。

```bash
VM_SA_EMAIL="eksmu-vm@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud storage buckets add-iam-policy-binding "gs://${COMPILE_BUCKET}" \
  --member="serviceAccount:${VM_SA_EMAIL}" \
  --role="roles/storage.objectCreator"

gcloud storage buckets add-iam-policy-binding "gs://${COMPILE_BUCKET}" \
  --member="serviceAccount:${VM_SA_EMAIL}" \
  --role="roles/storage.objectViewer"

gcloud run jobs add-iam-policy-binding "${JOB_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --member="serviceAccount:${VM_SA_EMAIL}" \
  --role="roles/run.jobsExecutorWithOverrides"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${VM_SA_EMAIL}" \
  --role="roles/run.viewer"
```

---

## 6. 网站环境变量与 systemd 定时器配置

### 6.1 环境变量（`/etc/eksmu/eksmu.env`）

在网站环境配置文件中启用编译并配置 Cloud Run Job 参数：

```dotenv
EKSMU_COMPILE_ENABLED=1
EKSMU_COMPILE_PROJECT=your-gcp-project-id
EKSMU_COMPILE_REGION=us-central1
EKSMU_COMPILE_JOB=eksmu-latex-compile
EKSMU_COMPILE_BUCKET=your-gcp-project-id-eksmu-compile-staging
```

### 6.2 systemd Service 与 Timer 样例

创建 `/etc/systemd/system/eksmu-compile-queue.service`：

```ini
[Unit]
Description=Eksmu LaTeX Compile Queue Worker
After=network-online.target

[Service]
Type=oneshot
User=eksmu
Group=eksmu
WorkingDirectory=/opt/eksmu/current
EnvironmentFile=/etc/eksmu/eksmu.env
ExecStart=/usr/bin/python3 -m eksmu --db /var/lib/eksmu/eksmu.sqlite run-compile-queue --limit 10
Nice=10
```

创建 `/etc/systemd/system/eksmu-compile-queue.timer`（每 2 分钟执行一次）：

```ini
[Unit]
Description=Run Eksmu LaTeX Compile Queue Every 2 Minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=2min
AccuracySec=15s
Persistent=true

[Install]
WantedBy=timers.target
```

启用定时器：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now eksmu-compile-queue.timer
```

---

## 7. 费用估算

参考 Google Cloud Run 官方定价页面（查询日期：2026-04-19，链接：<https://cloud.google.com/run/pricing>）：

- **Tier 1 区域（如 `us-central1`）Cloud Run Jobs 计费单价**：
  - CPU：`$0.000024 / vCPU-second`（免费额度：每月前 180,000 vCPU-seconds）
  - 内存：`$0.0000025 / GiB-second`（免费额度：每月前 360,000 GiB-seconds）
- **单次编译规格与耗时估算**：
  - 配置为 `2 vCPU + 4 GiB RAM`，假设平均每次 LaTeX 编译耗时 `30 秒`：
  - 每次消耗：`60 vCPU-seconds` + `120 GiB-seconds`
  - 超出免费额度后的单次成本：`60 × $0.000024 + 120 × $0.0000025 = $0.00144 + $0.00030 = $0.00174 / 次`（约 0.17 美分/次）。
- **月度费用上限估算**：
  - 正常月度规模（如每月 1,000 次编译，总计 60,000 vCPU-s 与 120,000 GiB-s）：完全在 Cloud Run 每月免费额度之内，计算费用约 **$0.00/月**（加上 Artifact Registry 与 GCS 暂存桶 7 天生命周期存储约 **$0.10–$0.50/月**）。
  - 即使达到全站每日 200 次硬限流上限（每月 6,000 次，平均 30s/次）：约 **$8.60/月**。

---

## 8. 安全说明与剩余风险

1. **为什么关闭 shell escape 后 TeX 无法执行外部命令或联网**：
   - TeX 引擎本身不具备套接字或 HTTP 网络客户端原语；TeX 唯一能调用外部程序（如 `curl`、`wget`、`sh`）的机制是 `\write18`（即 shell escape，包括受限的 `restricted shell_escape`）。
   - `compile_entry.py` 同时通过四重机制禁用外部命令：
     1. 环境变量强制设置 `shell_escape=f`、`openin_any=p`、`openout_any=p`；
     2. `latexmk` 命令行显式传递 `-no-shell-escape` 与 `-latexoption=-no-shell-escape`；
     3. 编译前静态扫描源码（剔除注释后拒绝 `\write18`、`\openin`/`\openout`/`\input` 指向绝对路径或 `..` 等危险原语）；
     4. 开启 `-recorder` 并在编译后审计 `.fls` 文件访问记录，一旦发现任何读取工作目录与 TeX Live 发行版目录之外的文件、或写入工作目录之外的文件，立即判为失败并丢弃产物。
2. **为什么编译容器拿不到正式附件桶与网站数据库**：
   - 网站 SQLite 数据库保存在网站虚拟机本地磁盘（`/var/lib/eksmu`），不向外部网络暴露。
   - 编译容器运行在无状态的 Cloud Run Job 沙箱中，挂载的专用服务账号 `eksmu-tex-compile-runner` 仅拥有 `EKSMU_COMPILE_BUCKET` 的 `objectViewer` 与 `objectCreator` 权限，对正式附件桶 `EKSMU_ATTACHMENT_BUCKET` 无任何 IAM 权限。
   - `compile_entry.py` 在获取元数据令牌并下载源码前会调用 `os.environ.clear()` 清空环境变量，且僅将只读/受限环境传给 `latexmk` 子进程。
3. **剩余风险与缓解**：
   - **同任务内对编译桶的覆盖风险**：拥有 `COMPILE_BUCKET` `objectCreator` 权限的容器若在极端情况下被突破（例如 TeX 引擎本身的内存破坏漏洞），攻击者理论上可尝试向 `COMPILE_BUCKET` 写入伪造对象。缓解方式：网站调度器在下载 `result.json` 与 PDF 后严格校验 `%PDF-` 头部、文件大小上限与 SHA-256，且可进一步为 Cloud Run Job 增加 VPC Direct Egress / Serverless VPC 规则或使用签名 URL 替代桶级 IAM。
   - **CPU/内存消耗（拒绝服务）**：恶意宏展开（如 `\def\a{\a}\a`）或超大 PDF 生成。缓解方式：进程组级 PDF 1500 秒与 HTML 300 秒硬超时（`SIGKILL`）、PDF 64 MiB 与 2,000 页上限、Cloud Run Job 1800 秒任务超时、网站队列 35 分钟总超时、`--max-retries=0`，以及每作者同时 1 个任务/每日 20 次、全站每日 200 次配额限制。
