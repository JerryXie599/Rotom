# 卡:令牌(JWT/会话)与沙箱逃逸

**指纹**:① 登录态是 JWT(cookie/Authorization 里 `eyJ...`,三段点分);② 目标提供"在线跑代码"的能力
(代码执行沙箱、公式/模板/规则引擎、`{"code": "..."}` 这类接口);③ 报错里出现 `vm2` / `jsonwebtoken` / `PyJWT` / `firebase/php-jwt`。

这三类问题的共同点:**不是"打进去",而是"绕过校验"** —— 校验逻辑本身有语义漏洞,不需要凭据。

---

## 一、JWT 签名类攻击(按"目标用什么算法"选路)

| CVE / 手法 | 适用条件 | 怎么验 |
|---|---|---|
| **alg=none**(CVE-2015-9235) | 库允许 `none` 或不强制验签 | 把 header 改成 `{"alg":"none"}`、去掉签名段提交,看是否通过 |
| **RS256 → HS256 密钥混淆**(CVE-2016-5431) | 服务用 **RS256 公钥**,但验签时按 header 的 alg 走 HMAC | 拿公钥当 HMAC 密钥重签:`jwt.encode(payload, public_key_pem, algorithm="HS256")`;先确认公钥能拿到(`/jwks.json`、源码、`.well-known`) |
| **jwk 头注入**(CVE-2018-0114) | 库信任 token 自带的 `jwk` 头 | header 里塞自签 RSA 公钥(`jwk`),用对应私钥签名;旧版 `jsonwebtoken`/`node-jose` 会用它验签 |
| **空签名 / 去掉签名段**(CVE-2020-28042) | 库把"签名段为空"当合法 | 提交 `header.payload.`(尾部空) |
| **kid 注入**(路径穿越/SQLi/命令注入) | `kid` 被拼进文件名/SQL/命令 | `kid=../../../../dev/null`、`kid=|command`、`kid=' UNION SELECT ...`;常与"拿服务端密钥文件"配合 |
| 弱密钥爆破 | HS256 + 弱口令 | `hashcat -m 16500` / `jwt_tool`;字典先试 `secret`、服务名、`changeme` |
| firebase/php-jwt 旧版 | PHP 站 + JWT | 条目名 `CVE-2021-46743`(未核实)按官方公告核对再用 |

**先做的事**:解出 header 看 `alg`/`kid`,再看有没有 `jwks.json`/公钥可拿 —— **有条件打混淆,没条件才爆破**。

## 二、Node 沙箱逃逸(vm2 / vm / 模板引擎)

**指纹**:接口像 `POST / {"code": "..."}`(常有长度上限,如 4096 字符);响应里会回显 `require` / `process` 未定义之类的错误。

| CVE / 手法 | 适用版本 | 一句话原理 |
|---|---|---|
| CVE-2022-36067 | vm2 ≤ 3.9.10 | `Error.prepareStackTrace` 相关逃逸 |
| CVE-2023-29017 | vm2 ≤ 3.9.14 | 同上,`prepareStackTrace` 变体 |
| CVE-2023-32314 | vm2 ≤ 3.9.17 | `Proxy` + handler 的 `getPrototypeOf` 逃逸 |
| CVE-2023-37466 | vm2 ≤ 3.9.19 | `Promise` 微任务里拿 `host` 对象 |

**通用验证流程(不依赖具体 payload)**:
1. 先探沙箱能力边界:`typeof require`、`typeof process`、`this.constructor.constructor("return process")()`、
   `globalThis.process`、`import("fs")` —— 看哪条通、报错信息泄露了什么;
2. **拿到 `process.mainModule.require` / `import()` 之一就能读文件**(`fs.readFileSync('/flag.txt')`);
3. 版本能从报错栈/`user-agent`/`package.json` 读到 → 直接按表挑对应明确版本区间的 payload;
4. 版本读不到时,从**旧到新**逐个试(旧版本的洞在新版被补,反之不成立);
5. **别在 thinking 里手推 payload** —— 写脚本打表试,一次跑完所有候选。

**其它同类沙箱**(同样按"先探边界"的顺序):Node `vm`(非 vm2)默认不是安全边界;Python `eval` 沙箱(用 `().__class__` 链);
模板引擎(SSTI → 见 `cards/bypass-filters`);正则/表达式引擎(SpEL/OGNL → 见 web-midware 的反序列化小节)。

## 三、反序列化闸门(pickle / torch.load / 语言原生)

| 目标 | 闸门 | 说明 |
|---|---|---|
| Python pickle | 完全无限制 | 有 `pickle.loads` 就等于 RCE(构造 `__reduce__`) |
| `torch.load(..., weights_only=True)` | ≥ 2.6 才真正安全 | **≤ 2.5.1 有一条绕过(CVE-2025-32434)**;先读 torch 版本(`python -c "import torch;print(torch.__version__)"` 或容器内 `pip show torch`) |
| Java `readObject` | 取决于 classpath | 先看依赖里有没有 ysoserial 可用链(commons-collections 等) |
| PHP `unserialize` | 取决于类 | 找 `__destruct`/`__wakeup` 里的危险操作 |
| Node `node-serialize` | 完全无限制 | `{"rce":"_$$ND_FUNC$$_function(){...}()"}` |

## 边界

- 令牌题常常**只需要改一个字段就能越权**(`role`/`uid`/`sub`),不需要伪造签名 —— 先看 payload 里有什么,别一上来就爆破。
- 沙箱题先把"能拿到什么原语"列清楚(读文件?执行命令?出网?),再决定要不要花时间在逃逸上;
  **只读一个文件时,文件读原语通常比 RCE 更短**。
- 版本区间/条目名拿不准的,按 `find-cve.md` 核对上游公告;⚠ 候选条目不要直接打。