# 涉诈网站侦查手册(shezha)

三条铁律见系统提示词;本手册 = 开工检索顺序 + 高频突破族速查 + 出口判读坑。完整弹药:`python3 tools/kb.py search "<关键词>"`(已挂载 武器库/47类武器 与 00_总控/技战法汇编)。

## 开工检索顺序(每个目标照此走,别跳步)
1. **判活**:经代理 HTTP 探测。502 且带 `Proxy-Connection`=代理自答(目标未必死);全族域 TLS `unexpected eof` 而第三方 200=出口 IP 被目标拉黑,换出口;死域"复活"但是停放模板=出口 DNS 劫持,DoH 双源复核。
2. **指纹归因**:响应头/Server/favicon/前端特征定底座(防封壳族:TSC/KK/guard/MiChat三跳/Xcdn/punycode/白名单网关301/斗篷三层链)。kb search "指纹 归因"。
3. **面普查(只读)**:`dirsearch -u <URL> -e php,html,js,bak,zip,sql,json,txt --threads 10`;前端 JS 抓接口 `curl -s URL | grep -oE '"/api/[^"]+"'`;常规位 `/actuator` `/swagger` `/druid` `/config` `/api/config` `/robots.txt` `/.well-known/`。
4. **按族出手**(下表),每步证据落盘(响应原文+时间戳),确认一个记一个 finding。
5. **利用面强制作业(先攻后收,2026-10-16 用户定版)**:普查≠产出。逐条过并留证:SQLi(确认→武器库 EXPLOIT-01 切片拖库)→上传落盘 getshell 三判→反序列化/SSTI/命令注入→已知组件 CVE(nuclei)→管理台弱口令与预填凭据(AUTHN-04/08)→已泄露 AK/SK 只读验证升级。侦察预算≤1/3;全部排除(逐条列排除证据)才能收面;收工必答"离 shell/拖库近了哪一步",纯普查+泄露堆 finding=不合格。

## 高频突破族速查(一句话一族;细节 kb search 武器名)
- AUTHZ-01 零鉴权后台整库拖:零鉴权入口 + accountId/_team_id 越权参数;翻页参数逐个试(pageNo 坑)。
- AUTHZ-03 未鉴权 config/secret:`common/init`、`config/get`、`check.json`、`logoConfig`、`data.json`;普查=GET/POST 双方法过一遍端点表。
- AUTHZ-05 Spring 暴露面:actuator(heapdump/env/heap 里 grep 密钥)、nacos 未授权、tenant/list。
- AUTHZ-07 游客 token:万能游客 token 多 SaaS 共用;活 token 必须从**活大厅域 server-render 页**抓(静态模板里的值是坑)。
- AUTHZ-08 云桶:先拉清单再打;列举 403 但精确键可能公开读 + index 兜底;COS 451=已被处置的 IOC。
- AUTHN-01 登录三态 oracle:回显能区分"用户不存在/密码错/已锁定"即可枚举;admin 口不校验密码=拦截先于校验。
- AUTHN-04 弱口令喷洒:先查锁窗模型(无锁/5次短锁5min/6次30min/万分钟锁);管理台 SPA「源码预填凭据」检索法(RouteLogin.vue 演示账号)。
- AUTHN-08 DjangoAdmin:凭证命中后 `?p=N` 翻页整表固化;会话 1h 失效要续期;strict 限速。
- CAPTCHA-06 hint 接口直读答案/算术码 ddddocr;CAPTCHA-07 摆设验证码(纯前端 GVerify、`/Admin` 大写绕过)。
- CRYPT 密钥提取:前端 chunk 全量拉取 + 16 类密钥指纹 grep(kb search "密钥提取 方法");信封协议先复刻再解(kb search "AES ECB 信封")。
- EXPLOIT-01 SQL 报错注入快通道:UPDATEXML/EXTRACTVALUE 单请求 31 字符(.NET dev_msg 直吐 MySQL 错误);确认后 30 字符切片拖库。
- APK-07 情报流水线:jadx 端点全图 + 实名五源(ICP/App Store/证书 CN/Google Ads/RDAP);APK-03 Flutter main.dart.js 常量直读免脱壳。
- INFRA-04 指纹串并:同底座站点互证(oss.pem 逐字节相同=同底座),一域突破全族受益。

## 实名与调证线(侦查的最终产出之一)
注册人/主体:ICP 备案、App Store 开发者、证书 CN、Google Ads、RDAP 五源交叉;资金:链上地址追踪(kb search "链上");AS/CDN 归属;同簇基建(共享桶/证书/网关)。

## 纪律(报告审阅口径)
- 只有拿到 shell/拖到库才准写"打穿/拿下";否则一律中性词(泄露/可读/还原/打通)。
- 证据原话级:时间戳+次数+响应原文,注明出自哪个文件;最小样本,不整库落盘。
- 正规资产/停打名单命中:停手,info finding + 转通报处置建议。

## 八大技战法(总纲;完整读 00_总控/技战法汇编)
剥壳寻真(斗篷壳解两层找真源)/ 三态探针(判活·判停放·判重建)/ 验证码突破 / 裸奔直取(零鉴权面普查) / 白盒复刻(前端加密协议复刻后离线打)/ 指纹串并(同底座归行)/ 资金穿透 / 常驻盯守(蹲守换域名,起停必登记)。

## 出口实例谱(多出口轮换时;严禁 pkill mihomo、禁共用实例)
18787 主实例 · 17879 备用 · 18802/18803/18805/18806/18888 多燃烧器独立实例;切节点后必须 `curl -x <实例> http://ip-api.com/json` 复核真换了(存在集中式出口时段,切了同 IP);已认证端点回 520=该出口被目标封,剔除。
