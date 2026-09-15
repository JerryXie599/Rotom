# 密码学 playbook

## 1. 先识别编码(30 秒)
```bash
python3 - <<'EOF'
import base64, binascii
s = open('cipher.txt').read().strip()
print(len(s), s[:60])
for name, fn in [('b64', lambda x: base64.b64decode(x + '='*(-len(x)%4))),
                 ('b32', base64.b32decode), ('hex', binascii.unhexlify)]:
    try: print(name, fn(s.encode())[:40])
    except Exception: pass
EOF
```
特征:纯 0-9a-f 偶数长→hex;`=` 结尾→base64;大写+数字且长度 8 倍数→base32;只有 A/B 两种符号→培根;
`.`/`-` 组合→摩斯(见 misc)。

## 2. RSA 攻击清单(按性价比排序)
| 条件 | 攻击 | 方法 |
|---|---|---|
| e 很小、明文短 | 小公钥指数 | `gmpy2.iroot(c, e)` 开方 |
| 同 n 两个互素 e | 共模攻击 | `gmpy2.gcdext` 求系数后合并 |
| d 很小 | 维纳攻击 | 连分数逼近;或直接 `RsaCtfTool` |
| n 可分解 | 因数分解 | factordb / yafu;n 相近时在 `isqrt(n)` 附近试除 |
| 多组同 n | 广播攻击(Håstad) | CRT + 开根 |
| p、q 接近 | Fermat 分解 | `gmpy2.is_square((p+q)^2//4 - n)` |
| 给了 dp | dp 泄露 | `p = gcd(n, pow(2, e*dp, n) - 2)` |

一键尝试(离线自带):
```bash
python3 knowledge/vendor/RsaCtfTool/RsaCtfTool.py --publickey pub.key --uncipherfile cipher.bin
python3 knowledge/vendor/RsaCtfTool/RsaCtfTool.py --publickey pub.key --private > priv.pem
```
自己写脚本:`from Crypto.Util.number import bytes_to_long, long_to_bytes`(本机与 pwn64 都有)。

## 3. AES / 分组密码
- **ECB**:相同明文块→相同密文块;有可控前缀可逐字节爆破。
- **CBC**:IV 可控→翻转第一块(`c[i-1] ^= delta`);padding oracle→逐字节解密;密钥=IV 可搬运块。
- **CTR/流密码**:密钥流复用→`c1 ^ c2 = m1 ^ m2`,crib-dragging 猜词。
- 代码里用 `get_random_bytes`/`urandom` 才安全;用 `random.`/时间做种子→可预测。

## 4. 哈希与口令
```bash
hashcat -m 0 hash.txt wordlist.txt          # MD5(-m 0) / SHA1(-m 100) / NTLM(-m 1000)
john --format=raw-md5 --wordlist=wordlist.txt hash.txt
```
先按长度猜类型;有 salt 就把格式拼对;别暴力破解长口令,先试常见词表。

## 5. z3 约束求解(神器)
```python
from z3 import *
xs = [BitVec(f'x{i}', 8) for i in range(LEN)]
s = Solver()
for x in xs: s.add(x > 32, x < 127)          # 可打印字符
s.add(<把题目校验逻辑翻译成约束>)
print(s.check()); print(bytes([s.model()[x].as_long() for x in xs]))
```

## 6. 提速要点
- 先试 RsaCtfTool 与常见模板;不认识的手法用 `python3 tools/kb.py search "rsa 攻击"` 查。
- 题目 hint 往往直接指攻击类型,先读。
- 大数运算用 `gmpy2`(比纯 Python 快几十倍)。

## flag 格式(实测踩过)
前缀**不固定**,见过 `NSSCTF{...}`、`flag{...}`、`LitCTF{...}` 等。提交前先看题目描述/容器 banner/回显/附件里有没有格式提示。
若第一次被拒,**优先检查前缀**再改内容;同一格式别重复提交(错误次数多会被判失败,靶场上限 20 次)。

## 格攻击 / Coppersmith(部分密钥泄露、小根)

**可用工具**:
- **`fpylll`(已装在 pwn64,apt 的 python3-fpylll)** —— 快后端,格攻击**必须在 pwn64 里跑**:
  `orb -m pwn64 bash -lc 'cd <工作区> && python3 tools/crypto/coppersmith.py ...'`(macOS 本机没有 fpylll,会退回 sympy 慢 2 倍);
- `sympy`(`Matrix.lll()`,纯 Python 兜底)、`gmpy2`、`pycryptodome`;
- **没有 `sage`**(源里没有该包,装不上)、没有 `olll`。需要 sage 才能秒解的场合,只能用上面的组合慢慢算。

### 现成工具(已实测可用)
```bash
# 已知 p 的高位和低位,求中间未知段(模未知因子,beta=0.5)
python3 tools/crypto/coppersmith.py     --n <N> --coeffs "<常数项>,<一次项系数>" --beta 0.5 --xbits <未知位数> [--m 8]
# 例:p_high*2^(low+mid) + p_low + x*2^low ≡ 0 (mod p)
#   --coeffs "p_high*2^(low+mid)+p_low 的值, 2^low 的值"  --beta 0.5 --xbits mid
```
作为库:
```python
import sys; sys.path.insert(0, "tools/crypto")
from coppersmith import coppersmith_univariate
roots = coppersmith_univariate([c0, c1], N, beta=0.5, X=1 << mid_bits, mm=8)   # 返回小根
```
- **首一化**:工具会自动乘 LC 的模逆使其在 mod N 下首一(非首一多项式必须先做这步);
- **参数**:m 越大可解的未知位数越多但格越大越慢;t 默认 `floor(d*m*(1/beta-1))`;
- **性能(实测)**:512 位 n、150 位未知、m=6(dim=12)→ **pwn64+fpylll 约 58 秒**(在 macOS 本机走 sympy 要 ~2 分钟);
  1024 位 n、459 位未知需要 m≈10(dim≈20)→ 会明显更慢(可能十几分钟)。**先跑小参数试,再逐步加大**。
- 注意:**不要**为了提速把格元素对 N 取模 —— 这个构造(N^{m-i} 族)必须用精确系数,取模会算错(实测取模后 3.9 秒但找不到根)。

### 慢任务怎么跑(重要)
格攻击动辄几分钟,不要在前台干等(worker 有单题限时,超时会被中断):
```bash
orb -m pwn64 bash -lc 'cd <工作区> && nohup python3 coppersmith_run.py > copp.log 2>&1 &'
# 过一会儿回来看:tail -20 copp.log
```
把中间结果写进文件,即使这次尝试被超时打断,下一轮也能接着算(工作目录会保留)。

### 判断能不能用 Coppersmith
- 未知比特数 < N 的位数 × 0.25 左右(部分密钥泄露,beta=0.5 时)才有希望;
- p 的未知段太长(例如 1024 位 n 里未知 600+ 位)→ 直接放弃这条路,考虑其它攻击;
- 需要 sage 才能秒解的场合,本机只能慢慢算 —— **提前判断可行性比硬算重要**。
