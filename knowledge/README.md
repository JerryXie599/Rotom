# CTF 知识库(Agent 解题时随查随用,离线)

现场无外网,知识库全部落盘在本目录。**Agent 通过 `tools/kb.py` 检索调用**:

```bash
python3 tools/kb.py list                          # 看知识库有什么
python3 tools/kb.py search "sql 注入 绕过"        # 关键词检索(中英文、多词=都要命中)
python3 tools/kb.py show pwn                      # 看某方向 playbook 全文
python3 tools/kb.py grep "0x67452301"             # 搜常量/特征串
```

| 文件 | 内容 |
|---|---|
| `web.md` | 网站安全:侦察清单、常见漏洞、payload 速查 |
| `pwn.md` | 二进制漏洞利用:checksec→定位→利用、pwntools 模板、常见坑 |
| `reverse.md` | 逆向工程:静态/动态分析、按语言分类、算法识别 |
| `crypto.md` | 密码学:RSA/AES/流密码攻击清单、z3/RsaCtfTool |
| `forensics.md` | 取证分析:流量/磁盘/内存/隐写/文档/日志 |
| `misc.md` | 杂项:编码、压缩包、二维码、沙箱逃逸 |

## 离线资料(vendor/,仓库里不含,用脚本拉取)

仓库出于体积考虑不含 `vendor/`(约 170MB),执行下面脚本即可重建:

```bash
./knowledge/fetch_vendor.sh
```

包含:`PayloadsAllTheThings`(Web payload 大全)、`ctf-wiki`(中文系统知识)、`RsaCtfTool`(RSA 自动攻击)。
拉完跑一次 `python3 tools/kb.py reindex` 让索引包含它们。

## 环境提示

- 本机是 macOS arm64,**跑不了 amd64 二进制**;二进制题一律 `orb -m pwn64 <命令>`
- `orb -m pwn64` 工具齐全(gdb/pwntools/radare2/binwalk/hashcat/john/tshark…),apt 可用
- `orb -m kali` **没有外网、工具很少**,别在它上面浪费时间
- 比赛接口域名强制直连(不受代理影响);模型是否走代理见 `.env`
