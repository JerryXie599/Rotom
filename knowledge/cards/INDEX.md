# 技术卡索引(指纹 → 卡)

**用法**:先按指纹/组件名命中卡,再精读该卡的最短路径,不要一上来全库检索。
`python3 tools/kb.py hit "7860"` / `kb.py hit "langflow"` → 命中的卡;`kb.py show cards/<卡名>` 看全文。

> 全部内容是我们自己写的**公开技术知识**(组件 → 验证方法 → 利用思路),**不含任何题目答案、题号或平台内部数据**;
> 具体 payload 请以厂商公告 / nuclei 模板 / 自己实测为准。

| 卡 | 什么时候看(指纹/场景) |
|---|---|
| [cve-fingerprint.md](cve-fingerprint.md) | 响应头/图标/JS/版本暴露了中间件或组件名 —— 先查已知 CVE 最短路径 |
| [jdwp-rce.md](jdwp-rce.md) | 出现 5005/8000 调试端口、Java 进程带 `-agentlib:jdwp`、报错里带 JDWP |
| [sandbox-escape.md](sandbox-escape.md) | 代码执行被限制在沙箱里(Python pickle / Node vm/vm2 / 模板引擎) |
| [cloud-storage-identity.md](cloud-storage-identity.md) | 云侧:S3/对象存储、SAS、serverless 配置、JWT 签发(JKU/JWKS)、设备码换 token |
| [lateral-chain.md](lateral-chain.md) | 单点已拿下、要串成链(SSRF/LFI → 读源码 → 凭据 → webshell → 内网 → 提权) |
| [session-and-captcha.md](session-and-captcha.md) | 登录有验证码、JWT/签名 cookie 可用 —— 用服务端会话/签名语义取巧(不是 OCR) |
| [bypass-filters.md](bypass-filters.md) | payload 被 WAF/黑名单拦,回显"被过滤/危险字符" |
| [php-deserialization.md](php-deserialization.md) | 出现 unserialize/phar:// 或 `Class not found` 报错 —— PHP 反序列化手搓链与 class-name oracle |
| [http-request-smuggling.md](http-request-smuggling.md) | 多级代理/网关、Server 不一致、403 来自代理、题面提到 outdated proxies —— 请求/响应走私与 desync |
| [device-admin-panel.md](device-admin-panel.md) | 路由器/网关/设备管理界面 —— 默认凭据(有序慢速)、配置导出、命令注入 |
| [rate-limit-safe-scanning.md](rate-limit-safe-scanning.md) | 开始高强度扫描前 / 目标突然变慢超时 —— 并发档位、过载处置、别把靶场打挂 |
| [reverse-analytic.md](reverse-analytic.md) | 逆向/二进制:状态机+密钥流、自解密壳、弱哈希碰撞、内存 dump、越界读 |
