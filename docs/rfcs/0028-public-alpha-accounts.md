# RFC0028：公开 Alpha 的邮箱确认与认证边界

日期：2026-10-06。状态：已实现源码与合成测试，真实域名、TLS 与 SMTP 待验收。用户已要求网站尽快上线，源码仓库与原私有数据继续保留。

## 运行模式

私有模式保留已交付邮箱账户与旧恢复文件兼容。显式 `--public-origin https://eksmu.com` 选择公开候选模式：应用仍绑定回环，由指定域名的受信代理接入。这个参数不创建公网入口，也不证明 DNS、TLS、邮件或网站已上线。

`GET /api/auth/status` 返回 `public_registration`、`require_verified_email` 与实际 `email_recovery_available`。公开模式不使用旧 `/api/actors` 或 `/api/auth/register`、`login`、`rotate` 入口；账户写入必须匹配完整路由，不能由任意嵌套路径的末段选择操作。未验证邮箱的会话不能操作公开实例。公开使用新数据库，不迁入原私有账户与资料。

## 两步注册

1. `POST /api/auth/email-register`：公开前端提交 `{email,label}`，也接受仅 `{email}`。兼容旧四字段请求时丢弃初次请求的姓名、密码与标签。响应统一为 `{request_received:true,verification_required:true}`；已存在、冷却期及排队失败不透露邮箱归属，不返回作者或登录 Cookie。无发信服务时返回 503，不建立账户。
2. `POST /api/auth/confirm-registration`：邮箱持有人提交 `{email,code,alias,password,label}`，在这一阶段明确设置显示名和密码。确认代码有效且一次性消费后，才原子创建作者、已验证邮箱账户与设备会话，响应包含 actor/session/过期时间及 `email_verified:true`，设置 `Secure`、`HttpOnly`、`SameSite=Strict` Cookie。

待确认表只保存邮箱、代码摘要及过期时间，不保存未验证请求的密码或姓名。代码有效期 20 分钟，重复请求有 60 秒冷却；持有他人最先请求的代码也不能激活他人预设的密码。邮箱只支持当前已验收的 ASCII 格式，域名小写，本地部分保留；不声称支持国际化邮箱或不同邮箱商的所有规范化规则。

公开 SMTP 由单工作线程和 128 封容量的内存队列发送，注册和找回反馈不等待网络发送。统一“已接收”不代表实际送达，队列满、发信失败或进程关闭可能丢弃任务；作者可在冷却后重试。没有持久投递保证。密码和验证码不进日志或公开导出。

## 来源、频率与会话

公开认证仅接受回环代理提供的一个合法 `X-OIC-Client-IP`，普通 `Forwarded`／`X-Forwarded-For` 不决定限速来源。代理覆盖该头，清理来访者的同名与转发头，并覆盖可能继承的来源信任配置；后端端口不开放公网。本轮配置针对直接连接 nginx 的入口，不自动信任 CDN 或另一代理。

公开限速计数和私有 HMAC 密钥保存在主数据库，使用独立事务，因此失败认证也消耗计数，进程重启后不会清零。计数有总容量上限；从较旧备份恢复仍可能回退，所以入口另有限速和总并发限制。它不是发表的学术评分或机构资格检查。

撤销、过期或未验证会话在状态查询中变为匿名并清理 Cookie；退出幂等。真正受保护的读写仍拒绝无效会话。改密在同一事务作废已有邮件代码，避免旧找回代码覆盖新密码；既有会话撤销及作者归属规则继续适用。

## 保存与核验

完整备份包含新的私有认证计数表与待确认表。恢复销毁所有待注册代码，保留计数快照；会话撤销及较新权威账户状态核对仍按原恢复契约执行。作品导出不包含这些表或邮箱。

83 项认证、HTTP 和备份定向测试实际覆盖：邮箱确认后才建作者、预注册接管拒绝、嵌套旧入口拒绝、无 SMTP、代理头缺失/重复/非法、重启保留限速、过期/重放代码、异机撤销后的匿名/退出、改密销毁旧找回代码、恢复销毁待注册代码及有界邮件队列。完整回归、浏览器与真实入口结果分别记录，见项目状态；合成邮件不计真实 SMTP 验收。

设计核对来源：[OWASP Authentication](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)、[OWASP Forgot Password](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html)、[nginx 请求头配置](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_set_header)。本轮改进不代表专业生产安全审计已完成。
