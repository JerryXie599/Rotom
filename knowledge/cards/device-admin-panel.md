# 卡:设备/路由器管理面板(默认凭据 + 配置导出 + 命令注入)

**适用**:目标是路由器/网关/设备管理界面(标题含 Router/Gateway/Management、端口 80/443/8002/8080/8443、
登录页很"设备风"、`Server: lighttpd/boa/goahead`、或 401 Basic 认证)。

> 口径:公开方法论;**凭据表是通用装备**,与具体题目无关。

## 0. 30 秒指纹(决定后面走哪条)
```bash
curl -k -s -i "http://T/" | head -15                       # Server / WWW-Authenticate / 跳转路径
curl -k -s "http://T/" | grep -oiE '<title>[^<]*' | head
for p in /login /admin /cgi-bin/ /goform/ /HNAP1 /setup.cgi /index.htm; do
  printf '%-18s %s\n' "$p" "$(curl -k -s -o /dev/null -w '%{http_code} %{size_download}' "http://T$p")"
done
```

## 1. 凭据:先"有序 + 慢速",别裸爆
- 用凭据墙的装备:**厂商默认表 → 跨轮已观测 → weak-leet/weak-affix → 产品词×年份 → 通用高频**:
```bash
python3 /app/tools/dict_prep.py --vendor <厂商> --out pw.txt   # 有指纹就给厂商
python3 /app/tools/dict_prep.py "router" --out pw.txt          # 没指纹:用产品词根
```
- **限速窗口先量**:连续 5 发看是否 429/断开;设备常 1~5 发/秒,串行投递 + 小延时;
  被限速就停 30~60 秒,再降速(并发 >1 常把设备打成不可用)。
- Basic 认证也走同一套表:`curl -u user:pass` 逐条试,注意 **401 vs 200** 的判别。

## 2. 配置备份/导出(比爆破更值钱)
- 常见路径:`/cgi-bin/export`, `/backup.cfg`, `/config.bin`, `/romfile.cfg`, `/NVRAM`, `/setup.cgi?todo=backup`
- 有的面板"导出配置"不需要认证 → 直接下载,配置里常有**明文或弱编码(admin/password 字段、base64/HMAC)**;
- 拿到配置后:找 `password=` / `admin_pass` / `wpa_psk` / `sysPassword`;解 base64 再试登录。

## 3. 命令注入/诊断接口
- 设备面板常有 ping/traceroute/DNS lookup/日志导出 —— 参数直接拼 shell:
  `;id`, `|id`, `$(id)`, 反引号;先确认是**回显型**还是**时间型**(`;sleep 3`)。
- 上传固件/恢复配置 = 高危写操作:先只读(读文件/读配置),拿到 flag 路径再评估是否需要写。

## 4. 鉴权绕过(设备固件常见)
- `X-Forwarded-For: 127.0.0.1`、`Referer` 同源、`/cgi-bin/` 与 `/api/` 的路径解析差异(`..;/`、`%2e%2e/`);
- 会话文件/令牌在 `/tmp` 或 cookie 里明文;有的设备支持"未登录访问 /admin/index.html 但接口校验在 js 里"。

## 5. 纪律
- 设备很脆:**并发 ≤5、遇到超时立刻降速**;打挂了整题就没了(实测有 agent 把 php-fpm/设备打 OOM)。
- 拿到入口后优先"读"(配置/文件/日志),再考虑命令执行。