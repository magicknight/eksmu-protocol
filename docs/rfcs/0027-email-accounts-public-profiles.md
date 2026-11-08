# RFC0027：邮箱账户与自愿公开的作者资料

2026-09-23，按用户对身份与机构要求的纠正实施于私有原型。用户同意下一步采用常见的邮箱＋密码登录、邮件找回，并让作者自行选择公开姓名与单位。此契约不授权公开网站或真实外部邮件测试。

## 权利和资料边界

- 发表、评论与修订的权限仍归稳定作者 UUID。没有机构、学历、推荐人、AI/Lean结果或付款门槛。
- 注册显示名称和公开单位由作者填写；可以用真实姓名、笔名，单位可留空、更新或清除。页面将单位标为“作者自述”，不自动核验或授予额外权限。
- 邮箱只用于本人登录、验证和密码找回。`/api/profiles`、社区发现、作品包、论文快照包和公开资料均不包含邮箱或密码哈希。公开资料可变，旧记录和固定发表快照仍保留提交时的作者信息，不回写历史。
- 旧恢复文件账户仍可登录，作者用当前有效恢复凭证绑定邮箱＋密码后，继续使用同一 UUID 和作品。绑定当时保留旧恢复凭证；**邮箱验证成功后**撤销旧恢复凭证和其他设备会话，当前设备保持登录。已验证邮箱的密码重设或改密也让旧恢复凭证失效。未验证邮箱的旧账户仍保留旧凭证作为恢复材料。

## 当前接口和数据

主库增加 `auth_passwords`、`auth_email_challenges`、`auth_profiles` 三张私有表；整实例备份、校验与恢复包含它们。恢复后所有会话仍先停用，离线账户核对从实际较新的权威库复制密码和公开资料状态，并丢弃旧邮件代码；较旧或同版本冲突的状态被拒绝。普通作品导出不包含这些表。

`POST /api/auth/email-register`、`password-login`、`attach-email`、`profile`、`change-password`、`request-verify`、`verify-email`、`request-reset`、`reset-password` 与 `GET /api/auth/profile`、`GET /api/profiles` 是本轮新增接口。修改公开资料使用版本号，旧页面冲突时须刷新核对。邮箱注册、登录和恢复沿用 HttpOnly、SameSite=Strict 的七天设备会话；当前 SSH 回环 HTTP 环境不加 `Secure`，既有可选 HTTPS 代理配置仍默认关闭。

密码使用每账户随机16字节盐的 PBKDF2-HMAC-SHA256，迭代600,000次，数据库不保存明文。它符合 [OWASP 密码存储建议](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)列出的 PBKDF2-HMAC-SHA256 工作参数；后续公开认证评审仍应评估资源成本、限速、泄露密码检查与算法升级。一次性验证和重设代码由安全随机数产生，数据库只存摘要，20分钟过期且使用一次；重设后旧会话全部撤销，并尝试发送通知。设计依据另见 [OWASP 找回密码建议](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html)和[邮箱验证建议](https://cheatsheetseries.owasp.org/cheatsheets/Email_Validation_and_Verification_Cheat_Sheet.html)。

## 私有运行与未完成条件

2026-09-27 源码补充了私有实例的进程内认证请求限速、较严格的 ASCII 邮箱域名检查，并统一找回请求在冷却期和发信异常时的 HTTP 响应。代码已在隔离源码上验收，尚未安装到现有 VM。重复邮箱注册仍会返回冲突，SMTP 同步发信仍可能产生时间差；公开前需要先验证邮箱再建立新账户、代理入口限速、真实发信与收信测试。详见 [账户说明](../guides/ACCOUNTS_README.md)。

当前实例没有配置 SMTP，因此邮箱和密码可注册、登录、由已登录者改密，**不能实际发送验证或找回邮件**。页面在注册前明确提示这一限制。服务只有在运行环境提供 `OIC_SMTP_HOST`、`OIC_SMTP_FROM`、`OIC_SMTP_USER`、`OIC_SMTP_PASSWORD_FILE`（可选 `OIC_SMTP_PORT`，默认465；587使用 STARTTLS）且密码文件属于服务用户并仅本人可读时才启用校验证书的 SMTP TLS 发送。配置和密码文件留在服务器私有位置，不进入 Git；本轮浏览器验收用进程内邮件测试接收器，没有真实外发。

未验证邮箱的账户在当前**私有试用**仍可用密码登录和发表，但邮件找回不可用。正式公开入口前须完成真实邮件验证、重复/滥用限速、抗账户枚举、邮箱地址范围与规范化策略、生产 HTTPS、凭证与发信配置验收；不能把私有演练称为生产认证验收。当前仅接受 ASCII 邮箱，域名按小写比较，本地部分按输入原样比较；这一临时范围需在公开前扩展和复核。机构自述无需邮件域名证明。
