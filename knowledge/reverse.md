# 逆向工程 playbook

> 环境:本机有 objdump/otool/nm/strings/lldb/gdb;**没有 IDA,也没有 Ghidra**。
> amd64 二进制放 `orb -m pwn64`(里面有 radare2);Java 用 jadx。

## 1. 先分类
```bash
file ./attachment && head -c 64 ./attachment | xxd | head -4
```
| 特征 | 类型 | 工具 |
|---|---|---|
| `ELF 64-bit` | Linux 二进制 | pwn64:`r2 -A`、`objdump -d`、`gdb` |
| `Mach-O` | macOS | 本机 `otool -tvV`、`lldb` |
| `PE32` | Windows | `objdump -d` / pwn64 `r2` |
| Zip + `.class` | Java | `jadx -d out app.jar` |
| `.pyc` | Python 字节码 | `python3 -m dis`、pycdc(若装了) |
| `APK` | Android | `apktool d` + `jadx` |
| Go/Rust 大静态 | — | `strings` 找符号 + `r2` 定向分析 |

## 2. radare2 速查(pwn64)
```bash
orb -m pwn64 bash -lc 'rabin2 -zz ./chall | grep -iE "flag|key|pass"'
orb -m pwn64 bash -lc 'r2 -qc "aaa; s main; pdf" ./chall'        # 一条命令出反汇编
# 交互: afl(函数) / s main / pdf / axt @ sym.imp.strcmp / iz(字符串) / VV(图形)
```

## 3. 动态分析(常比静态快 10 倍)
```bash
orb -m pwn64 bash -lc 'ltrace ./chall'      # 库调用:strcmp/memcmp 直接看参数
orb -m pwn64 bash -lc 'strace ./chall'      # 系统调用:读写了哪些文件
orb -m pwn64 bash -lc 'gdb -q ./chall -ex "b strcmp" -ex r -ex "x/s $rdi" -ex "x/s $rsi"'
```

## 4. 常见算法识别
| 特征 | 算法 |
|---|---|
| `0x67452301 0xefcdab89` | MD5 |
| `0x6a09e667 0xbb67ae85` | SHA-256 |
| 256 项初始化表 + XOR | RC4 |
| `0x9e3779b9`、位移 4/5 | TEA/XTEA |
| 大量 64 位异或+加法 | XXTEA / 简单异或 |
| 逐字节与常量表比较 | 直接 dump 常量表就是 flag |

## 5. 提速要点
- 先 `strings -a | grep -iE "flag|correct|wrong|key"` —— 很多题答案就在字符串里。
- 有 `.pyc` 先反编译;有 Java 先 jadx。
- 大型 Go/Rust 别全量分析,带着假设定位主函数。
- 不认识的算法/常量,用 `python3 tools/kb.py search "<常量或特征>"` 查资料。

## flag 格式(实测踩过)
前缀**不固定**,见过 `NSSCTF{...}`、`flag{...}`、`LitCTF{...}` 等。提交前先看题目描述/容器 banner/回显/附件里有没有格式提示。
若第一次被拒,**优先检查前缀**再改内容;同一格式别重复提交(错误次数多会被判失败,靶场上限 20 次)。

## 加壳与脱壳(实战要点)

### 识别
```bash
file packed.bin; objdump -h packed.bin | head        # 看节区名
# 典型节区名:UPX0/UPX1(UPX)、.aspack/.adata(ASPack)、.themida/.vmp0(VMProtect/Themida)
strings -a packed.bin | grep -iE "upx|aspack|themida|vmprotect|packed"
upx -l packed.bin 2>/dev/null                         # UPX 会自报
```
特征:入口点落在最后一个节区、节区可写可执行、代码熵很高、导入表极小。

### 脱壳思路(按难度)
1. **UPX**:直接 `upx -d packed.bin -o unpacked.bin`(最省事)。
2. **简单壳(ASPack 类)**:找 **OEP**(原始入口点)后内存 dump + 修 IAT:
   - x86 常用「**ESP 定律**」:入口处 `pushad` 后对栈写硬件断点,跑到 `popad` 附近即接近 OEP;
   - 无 x64dbg 时的替代:用 `gdb` 断在入口、`catch syscall`/单步跟到 OEP,再用
     `dump binary memory dump.bin <start> <end>`(或从 `/proc/<pid>/maps` 取段范围)。
3. **unicorn 模拟脱壳**(无调试器/纯脚本时):
   ```python
   from unicorn import *
   from unicorn.x86_const import *
   # 映射 PE/ELF 段 -> 在 OEP 附近设 hook 计数 -> 运行到解密完成 -> 把内存段 dump 出来
   ```
   适合"壳只做内存解密、不反调试"的情况。
4. **强壳(VMProtect/Themida)**:不要硬脱,改从**行为**入手——动态跑起来后 hook 关键 API
   (`ltrace`/`strace`/`LD_PRELOAD`),或直接看它解密后落到磁盘/内存的明文。

### 判据
脱壳成功的标志:节区恢复可读、导入表完整、`strings` 里出现原本被隐藏的明文字符串(常含 flag 线索)。
