# 卡:把单点串成链(横向 / 提权)

**适用**:已经拿到一个入口(SSRF / LFI / 上传 / 弱口令 / RCE),目标是"顺着往下走拿到最终目标"。

## 链条模板(按顺序推进,每步都写黑板)
```
① 单点   SSRF / LFI / 上传 / 弱口令 / 组件 CVE
② 读源码  LFI+php://filter、SSRF file:// 读站点源码、备份包/.git —— 源码里常直接有库凭据/内网地址
③ 落点   写 webshell(上传绕过 / SQLi OUTFILE / 日志投毒)、或直接 RCE
④ 内网   从落点看 /etc/hosts、路由、环境变量;用 bash /dev/tcp 或 curl 扫常见端口(别 nmap 全端口)
⑤ 横向   数据库凭据复用、SSH 弱口令(小字典)、中间件未授权(Redis/Mongo/ES)
⑥ 提权   SUID/capabilities(find / -perm -4000)、cron、可写服务配置、内核/组件版本
⑦ 目标   读真正的 flag 位置:容器内 /flag*、数据库表、后台页面、环境变量
```

## 关键技巧
- **日志投毒 → RCE**:能读日志(LFI)+ 能写日志(UA/参数进日志)→ 让日志里出现 PHP 代码 → 包含它;
- **SSRF 读源码**:`file:///var/www/html/index.php`,别忘了 `php://filter/convert.base64-encode`;
- **当跳板**:目标机器上如果有 PHP/Perl 等,可写一个转发脚本(端口转发到内网),比反弹 shell 稳;
- **内网扫描克制**:先 `/etc/hosts` + 常见端口(80/8080/3306/6379/22/5005),不要全端口狂扫。

## 纪律
- 每完成一步就在黑板写"已验证事实 + 下一步",否则换会话就断链;
- 写操作(改密码/加账号/上传)做完**立刻还原**并在黑板与 finding 里写明。
