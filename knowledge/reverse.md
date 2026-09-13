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
