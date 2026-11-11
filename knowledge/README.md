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
| `playbooks/credential-wall.md` | 凭据墙打法:判通道 → 量限速 → 用户名面差分 → 排序组表 → paced 投递 |
| `wordlists/` | 字典库:6 张厂商定向口令表 + 通用弱口令 + 用户名面 + 厂商词根表(详见其 README) |

## 离线资料(vendor/,仓库里不含,用脚本拉取)

仓库出于体积考虑不含 `vendor/`(约 170MB),执行下面脚本即可重建:

```bash
./knowledge/fetch_vendor.sh
```

包含:`PayloadsAllTheThings`(Web payload 大全)、`ctf-wiki`(中文系统知识)、`RsaCtfTool`(RSA 自动攻击)。
拉完跑一次 `python3 tools/kb.py reindex` 让索引包含它们。

## 环境提示

- **macOS(本地)**:本机 arm64 跑不了 amd64 二进制 → 二进制题走 `orb -m pwn64 <命令>`;
  那里工具齐全(gdb/pwntools/radare2/binwalk/hashcat/john/tshark…);`orb -m kali` 没有外网、工具很少,别在它上面浪费时间。
- **Linux 容器(托管运行)**:amd64 原生执行,工具全部本地可用(gdb/radare2/tshark/nmap(root)…),
  `orb -m pwn64 <命令>` 由兼容层接管、等价于本地执行 —— 老写法不用改。
- 两种环境都**没有外网**(托管模式只放行平台答题 API + 白名单模型网关):资料一律查本知识库,别指望联网。

## 新增(2026-09-28)
- `ops-bench.md` —— **跑分平台运维 playbook**(跨平台通用):封装先写、槽位原子周转、平台故障处置、
  收工以平台口径为准(不看时间)、证据与资产纪律、交接三行。**不含任何题目答案**。
- `cards/` —— **技术卡(指纹 → 最短路径)**:`python3 tools/kb.py hit "<指纹>"` 命中后精读一张。
  内容是我们自己写的公开技术(组件/端口/漏洞类别 → 验证方法),不含题号、答案或平台内部数据。
