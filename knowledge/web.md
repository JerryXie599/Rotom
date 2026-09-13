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

## flag 格式(实测踩过)
前缀**不固定**,见过 `NSSCTF{...}`、`flag{...}`、`LitCTF{...}` 等。提交前先看题目描述/容器 banner/回显/附件里有没有格式提示。
若第一次被拒,**优先检查前缀**再改内容;同一格式别重复提交(错误次数多会被判失败,靶场上限 20 次)。

## 框架 / 中间件特性(压测中真实卡住的点)

### Go net/http
- `http.FileServer` / `http.ServeFile`:目录列表由 `dirList` 生成;`/flag` 若被路由或 handler 拦截,先看
  是否存在**双重编码/大小写/`//`/`..%2f`** 之类的 path 归一化差异(`path.Clean` 与 URL 解码顺序)。
- **Range 越界读**:`Range: bytes=0-99999999` 会让 `ServeContent` 返回整个文件,但响应里
  `Content-Length` 会被校正为实际大小 —— 只拿到部分内容时,改用**不带 Range 的请求**或调整范围。
- 静态目录列出的文件名若含 `flag`,直接对每个路径试 `GET`、`GET ?download=`、`GET /dir/file%00.txt`。
- 常见坑:`http.Dir` 会跟随符号链接;`filepath.Join` 与应用层拼接不一致会产生穿越。

### 其他中间件常见差异
| 栈 | 考点 |
|---|---|
| Node/Express | `express.static` 穿越、`sendFile` 路径校验、原型链污染(`__proto__` 合并) |
| Java Spring | Actuator 端点暴露、路径匹配差异(`/;/`、`%2e`、大小写)、SpEL 注入 |
| PHP | `php://filter` 读源码、`phtml`/`.user.ini`、反序列化链 |
| Nginx/Apache | `alias` 目录穿越、CRLF 注入、`.htaccess` 覆盖 |

**思路**:框架题先看**版本**与**路由定义**,再对照该框架已知的路径处理差异;越界读不要只用一种 Range 写法。
