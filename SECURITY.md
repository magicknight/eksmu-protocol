# 安全策略与运行边界 (Security Policy & Operational Boundaries)

本文档说明 **Eksmu** 开源代码库的安全机制、运行隔离边界、已知局限以及安全漏洞报告方式。治理与参与权原则见 [CHARTER.md](CHARTER.md)。

---

## 1. 安全架构与已实现的防护

### 1.1 Web 服务、会话与认证限流
- **回环绑定与显式源校验**：应用服务端（`python -m eksmu serve`）默认仅绑定 `127.0.0.1`。生产环境通过显式传入 `--public-origin https://<domain>` 配合反向代理对外提供 HTTPS 服务，严格校验 `Host`、`Origin`、Fetch Metadata（`Sec-Fetch-*`）、请求体大小上限与超时，并启用 CSP（禁止外部脚本与内联脚本执行）。
- **认证与防枚举**：公开注册要求两步邮箱确认，未验证邮箱不创建作者记录（不泄露邮箱是否已注册）。会话 Cookie 强制启用 `Secure`、`HttpOnly`、`SameSite=Strict`。认证频率限制保存在主数据库独立事务中（`eksmu/auth_limits.py`），进程重启不会清零计数。详见 [账户说明](docs/guides/ACCOUNTS_README.md) 与 [RFC 0028](docs/rfcs/0028-public-alpha-accounts.md)。

### 1.2 上传内容与代码执行隔离
作者上传的正文、PDF、LaTeX 源码包与代码附件均被视为**不可信数据（Untrusted Data）**：
- **静态安全清洗**：Markdown/LaTeX 渲染输出经服务端白名单标签重建，不加载外部远程图片或脚本；LaTeX 源码包解析（`eksmu/manuscript.py`）拒绝路径穿越（`..`）、绝对路径、符号链接逃逸与危险 TeX 原语（`\write18` 等）。
- **无特权隔离 LaTeX 编译容器**：LaTeX 源码编译在独立容器沙箱（[deployment/compile/](deployment/compile/README.md)）中以非 root 用户（UID/GID `10001:10001`）运行，强制关闭 `shell_escape`、限制文件读写范围、审计 `.fls` 访问记录，且编译容器无权访问网站数据库或正式附件存储桶。
- **Linux 命名空间隔离 Lean 预检**：自愿单文件 Lean 4.34.0 检查在独立 `user/mount/pid/net/ipc/uts` 命名空间、只读工具链挂载、无宿主家目录、无网络与受限 `systemd-run` cgroup（内存、进程数、CPU 与墙钟超时）中执行（`eksmu/lean_sandbox.py`，详见 [RFC 0024](docs/rfcs/0024-private-lean-activity.md)）。

### 1.3 API 密钥隔离与非对称临时密封
- **平台密钥不进 Web 进程**：平台 AI 分析密钥仅由独立后台 Worker 进程从受限配置文件（`0600`）读取，Web 进程从不接触。
- **作者自带 API 密钥密封**：当作者使用自带 API 密钥请求深入分析时，Web 进程立即使用 Worker 的 X25519 公钥对密钥进行一次性密封（临时密钥交换 + `HKDF-SHA256` + `ChaCha20-Poly1305`，绑定请求 ID），数据库与备份中从不落地明文密钥；Worker 解密调用一次后无论成败立即擦除密文（详见 [RFC 0031](docs/rfcs/0031-ai-analysis-reports.md)）。

### 1.4 密码学记录链、离线核验与合规移除
- **追加式 SHA-256 记录链与比特币锚定**：公开记录与已对账附件摘要（`material`）按顺序写入 `eksmu.record-chain.v1`，链头定期通过 OpenTimestamps 锚定至比特币区块链，任何人都可用零依赖脚本 `tools/eksmu_verify.py` 与 `tools/proof_bundle.py` 离线核验。
- **隐私／合规附件移除墓碑（Redactions）**：当附件包含泄露的个人隐私或侵权内容需移除时，`redact-attachment` 清除原始字节但保留链上 `material` 摘要记录与公开移除声明，确保历史记录链与比特币锚点永不断裂（详见 [RFC 0032](docs/rfcs/0032-content-coverage-and-preservation.md) 与 [备份指南](docs/guides/BACKUP_README.md)）。

---

## 2. 已知局限与自建节点注意事项

1. **时间戳证明的客观边界**：OpenTimestamps 比特币锚点仅在密码学上证明某段字节不晚于锚定区块时间已经存在，不证明作者真实身份、原创性或科学结论正确性。
2. **操作系统级同用户边界**：单机默认部署下，Web 进程与分析 Worker 若以同一 Linux 用户运行，X25519 密钥密封主要防范数据库/备份泄露与 Web 路径误用，不能抵御已取得该操作系统用户完整执行权限的攻击者；高安全需求部署应将 Web 与各 Worker 拆分为独立操作系统用户或独立主机。
3. **自建公开实例要求**：Python 标准库 HTTP 服务器需配合前置反向代理（如 nginx，配置见 `deployment/google-cloud/public_alpha.py`）处理 TLS 终止、连接数限制与静态缓存，请勿将后端回环端口直接暴露至公网。

---

## 3. 报告安全漏洞 (Reporting a Vulnerability)

如果你在 Eksmu 代码库或线上服务中发现了安全漏洞（如认证绕过、跨站脚本 XSS、沙箱逃逸、路径穿越或隐私泄露）：

1. **请勿在公开 GitHub Issue 或公开讨论区直接发布未修复的利用细节（Exploit）**；
2. 请通过邮件将漏洞说明、复现步骤与影响范围发送至：**`noreply@eksmu.com`**（标题注明 `[SECURITY]`），或通过 GitHub 仓库的 **Security Advisories (Report a vulnerability)** 私下提交；
3. 维护者将在收到报告后尽快确认、修复并回归测试，修复上线后在更新记录中说明影响范围与致谢。
