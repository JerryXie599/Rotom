# 卡:云侧(存储 / 身份 / 配置)

**适用**:题目里有对象存储桶、SAS/预签名 URL、serverless/函数配置、JWT/JWKS、OIDC 设备码等云成分。

| 场景 | 指纹 | 最短路径 |
|---|---|---|
| 对象存储匿名访问 | 桶域名可解析(`*.s3.*` / `*.blob.core.windows.net`) | `aws s3 ls s3://bucket --no-sign-request`(或 curl 列桶接口);能读就读,能写就是高危 |
| 预签名/SAS 段注入 | URL 里带 `sig=`/`se=`/`sp=` | 试改 `sp=`(权限段)、`se=`(过期)、`sr=`(资源),看是否放宽(注意只用于**授权测试**) |
| serverless 配置泄露 | 函数 API 返回错误栈/环境信息 | 读 `/proc/self/environ`、环境变量接口、部署脚本里的角色 ARN |
| JWT 签发滥用 | `jku`/`jwks` 头可控 | 自建 JWKS(HTTP 可访问)→ 用自己私钥签 `kid` 对应的 key → 换 `jku` 指向自己的 JWKS |
| 设备码/OIDC 流转 | 出现 `device_code` | 按标准流程换 token,读敏感声明(`roles`/`groups`)→ 用高权限声明访问资源 |
| IMDS/元数据 | SSRF 可用 | `http://169.254.169.254/latest/meta-data/iam/security-credentials/` 取临时凭据 |

**纪律**:云侧"读"与"写"差别巨大 —— 只读验证优先;导出数据只截证明所需的片段。
