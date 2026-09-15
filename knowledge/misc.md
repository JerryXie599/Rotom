# Misc 杂项 playbook

## 1. 编码识别速查
| 特征 | 编码 | 解法 |
|---|---|---|
| `·`/`-` 长短组合 | 摩斯 | 替换成 `./-` 后解码,注意分隔符 |
| 只有两种字符、长度 5 倍数 | 培根 | 每 5 位映射字母 |
| 字母整体位移 | 凯撒 | 26 种全试,看可读词 |
| `=` 结尾 / base 变体 | base64/32/58 | 逐层解码(常套娃 3~5 层) |
| 全数字 | hex/十进制 ASCII/Unicode | 转字符;`&#x..;` 是 HTML 实体 |
| 二维码/条形码 | QR/Code128 | pyzbar、zbarimg 或手写解析 |
| 0101 二进制 | 二进制 | 转 ASCII |
| 乱码中文 | GBK/UTF-8 错位 | 换编码解码 |

```bash
python3 - <<'EOF'    # 多层解码起手
import base64, binascii
s = open('flag.txt').read().strip()
for i in range(6):
    for name, fn in [('b64', lambda x: base64.b64decode(x + '='*(-len(x)%4))),
                     ('b32', base64.b32decode), ('hex', binascii.unhexlify)]:
        try:
            n = fn(s.encode()); print(i, name, n[:60]); s = n.decode('utf-8','ignore'); break
        except Exception: pass
EOF
```

## 2. 压缩包
```bash
file a.zip; unzip -l a.zip; 7z l a.zip
zip2john a.zip > h.txt && john h.txt          # 真加密
bkcrack -C a.zip -c inner.txt -p plain.txt    # 已知明文攻击
```
嵌套压缩包:写脚本递归解压;遇密码先查题目提示或试常见词。

## 3. 沙箱 / pyjail
- 绕过黑名单:字符串拼接、`getattr`、`chr()` 拼关键字、编码后 `exec`。
- Python 起手:`().__class__.__bases__[0].__subclasses__()` 找 `os._wrap_close`/`catch_warnings`。
- 能直接 `open('/flag').read()` 就别费劲 getshell。

## 4. 其它形态
- 签到题:描述里直接给 flag。
- 文件头损坏:对照正确文件头修(PNG `89 50 4E 47`、JPG `FF D8 FF`)。
- 进制/时间戳/经纬度题:python 直接算。
- 游戏题:多半"通关即给 flag",找改内存/改存档/伪造请求的点。

## 5. 提速要点
- **先 `file` + `strings` + `binwalk`** 三连,再定方向。
- 编码套娃写脚本循环解,别手工一层层来。
- 标题/hint 往往就是解法(base/morse/zip 等词直接试),也可 `python3 tools/kb.py search "<hint 内容>"`。

## flag 格式(实测踩过)
前缀**不固定**,见过 `NSSCTF{...}`、`flag{...}`、`LitCTF{...}` 等。提交前先看题目描述/容器 banner/回显/附件里有没有格式提示。
若第一次被拒,**优先检查前缀**再改内容;同一格式别重复提交(错误次数多会被判失败,靶场上限 20 次)。

## 脚本/文档编码解码(本机已有工具,离线可用)

| 现象 | 判断 | 工具 |
|---|---|---|
| `#@~^......== ... ......==^#~@` | VBScript/JScript Encode(VBE/JSE) | `python3 tools/decoders/vbe_decode.py <文件或编码串>`(已是本地工具,直接可用) |
| Office 文档带宏 | VBA 宏 | `olevba file.doc` / `oleid file.doc`(oletools 已装) |
| PDF 里藏字符串 | PDF 对象流 | `pdf-parser.py`、`strings` |
| 多层文本编码 | base64/hex/base32 套娃 | 见本文 §1 的循环解码脚本 |

VBE 例子(实测):`#@~^DgAAAA==\ko$K6,JC V^GJqAQAAA==^#~@` → `MsgBox "Hello"`。
先 base64 解一层再遇到 `#@~^` 的,先解 base64 再喂给 vbe_decode.py。
**不要联网装工具**:断网环境下 `pip/apt/curl` 访问外网会被拒,缺工具就用本地已有实现或自己照算法写。

## 小心"诱饵 flag"(实测踩坑)
二进制/附件里出现的 `flag{...}` 字符串**可能是诱饵**(例如 `flag{tcache_vault_local_pwned}` 这种明显是假的),
真 flag 一般在**服务端的 flag 文件**里(拿到 shell 后 `cat flag* /flag`),或题目/平台上另有说明。
- 优先提交**从目标环境实际读到**的 flag(远程文件、数据库、接口返回),而不是从二进制里抠出来的字符串;
- 若提交被拒且你交的是"二进制里找到的",换去读服务端文件;正确 flag 通常形如 `NSSCTF{...}` / `flag{...}` 且带随机串。
