---
name: wanqu-ctf-agent
description: 操作 2026 湾区杯 AI 智能体解题赛(环节二/三)的自研 harness。适用于:启动或停止这个自动解题项目、改队伍 token 或模型配置、排查 runner/agent 不跑或跑得慢、看实时状态、给题目加知识库经验、按新版规定导出审计轨迹或打包交付、赛前用本地模拟比赛做压测。关键词:Rotom、湾区杯、智能体解题赛、start.sh、dashboard、runner、SHARED.md、审计打包。
---

# 湾区杯 AI 智能体解题赛 —— harness 操作手册

项目根 = 本仓库的检出根目录(即 start.sh / runner.py 所在目录),下称「项目根」。
比赛形式:**按一次「开始」后全程不再碰电脑**,agent 自动拉题→解题→提交;现场提供模型 key,
现场**无外网**,所有参考资料必须已落盘。

## 一、最常用操作

```bash
cd <项目根>
./start.sh                  # 一键:体检 → 起网页控制台 → 后台自动开跑(比赛当天就用这条)
./start.sh --check-only     # 只体检:token 长度 / 模型配置 / 接口连通性 / 工作区
./start.sh --practice       # 练习模式(平台已解出的题也重跑)
./start.sh --project round1 # 在指定项目(独立工作区)里跑,项目之间互不干扰
./start.sh status | stop    # 看状态 / 优雅停机(连所有 agent 一起收)
```

网页控制台(改配置、看实时状态都在这里,用户偏好不敲命令;右上角「☀️ 浅色 / 🌙 深色」按钮可切主题,选择记在浏览器 localStorage):`./dashboard.sh` → 打印的
`http://127.0.0.1:8799`(端口被占用会自动顺延,实际端口写在 `logs/dashboard.port`)。
关键:8188 附近的 **8788 被用户自己的 `llm-route.py` 占着**,别用。

## 二、配置(两个入口,等价)

```bash
python3 config.py                                    # 查看当前配置(密钥打码)
python3 config.py --token icqXXXX                    # 改队伍 token
python3 config.py --base-url URL --key K --model M --api-type openai-completions
python3 config.py --contest-base URL --contest-query-path /query --contest-reset-path /reset --contest-submit-path /submit
python3 config.py --test                             # 用 pi 实跑一次验证模型连通
```
- 配置全部落在 `.env`;**模型配置会自动同步成 pi 的 provider `rotom`**(不用手改 models.json)。
- API 类型四选一:`openai-completions`(最通用)/`anthropic-messages`/`openai-responses`/`google-generative-ai`。
- 比赛接口三个地址均可改:填相对路径(拼 `CONTEST_BASE`)或整条 URL(以 http 开头则原样使用)。

## 三、看状态

- 网页看板:题目状态徽章、「各 agent 实时工具调用与发言」、token、静默时长(>180s 标"可能卡住")、
  `SHARED.md` 共享黑板、提交记录、事件流;2 秒自刷。
- 终端:`tail -f logs/runner.out`(调度)、`tail -f logs/submissions.log`(提交)、`python3 tools/monitor.py`。
- 单题工作区:`work/<大类>-<题名>/`(如 `web-web01`),内含 `.qid`、`SHARED.md`、`files/`、`w1/`、`w2/`。

## 四、架构与关键文件

| 文件 | 职责 |
|---|---|
| `runner.py` | 调度主循环:轮询查题、广度优先派发、自适应降并发、重试与平台对账、Observer、孤儿清理、优雅停机 |
| `solver.py` | 单题 worker:建隔离工作区、下载解压附件、组装 prompt、起 `pi --mode json` |
| `board.py` | 黑板 `board.json`(flock),题目状态机 pending→running→solved/failed |
| `contest_api.py` | 三接口封装;`ContestError` 区分业务错误与网络错误 |
| `dashboard.py` | 网页控制台 + 实时看板 |
| `tools/` | `submit_flag.py`(agent 提交)、`reset_env.py`、`kb.py`(**知识库检索**)、`export_trace.py`(审计轨迹)、`package_submission.py`(交付打包)、`monitor.py` |
| `tests/` | `mock_contest_server.py`(同构本地接口)、`local_contest.sh`(本地压测)、`scale_test.py`(调度回归,不耗模型) |

## 五、必须知道的坑(踩过的)

1. **模型与代理**:micuapi 这类中转**必须走用户的本地代理**(直连被 reset),代理一关就完全不可用;
   `api.kimi.com/coding`(provider `kimi`,模型 `kimi-for-coding`)**直连可用**,是替代方案。
   **比赛相关域名(`*.ichunqiu.com`、`g.ichunqiu.com`)一律强制直连**——本机代理会破坏其 HTTPS(SSL_ERROR_SYSCALL)且让附件下载 `Connection refused`。
2. **kali 虚拟机是空壳且无外网**,别在 prompt 或方案里指望它;**pwn64 才是主力 Linux 环境**(工具齐全、apt 可用,IP `192.168.139.136`)。
3. **没有 IDA,也装不上 Ghidra**;逆向靠 radare2(pwn64)/objdump/lldb/strings,jadx 处理 Java。
4. **PWN 慢的根因是绕路**:agent 容易把时间花在搭本地复现环境(改 ld.so、LD_PRELOAD、找匹配 libc)。
   prompt 已写明:**先用 pwntools `remote()` 直接打远程,附件给了 libc 就直接用其偏移**。
5. **协同不会自发发生**:必须在 prompt 里强制(第一个动作读+写 `SHARED.md`、每 3~5 条命令写进展),
   runner 侧还有 Observer 兜底写入队友动作。
6. **平台关闭时**(返回 `{"code":203,"message":"比赛已结束"}`)点「开始跑」只会持续轮询,这是**预期行为不是故障**。
7. 本地服务必须 `nohup`/`setsid`,否则启动它的终端退出后服务被 SIGHUP 带走;端口要做自增回退。

## 六、按新版规定交付(硬性)

规则第 7 条:挑战结束后工作人员收走智能体做复测与审计,需交部署包 + README(环境依赖 / 配置启动 / 日志审计路径)。

```bash
python3 tools/export_trace.py        # logs/*.jsonl → traces/*.md(Thought/Action/Observation 三段式)+ SUMMARY.md
python3 tools/package_submission.py  # → dist/rotom-agent-<项目>-<时间>.tar.gz(含合规 README、脱敏 .env.example、traces)
python3 tools/package_submission.py --zip            # 追加 .zip 格式
```
审计对应关系:工具调用=`tool_execution_start`(Action)、工具输出=`toolResult`(Observation)、模型发言=`message_end`(Thought)。
**`MAX_LOG_MB` 默认为 0(不清理日志)**,因为审计需要完整轨迹;磁盘紧张时才设数值,且先导出轨迹。

## 七、赛前用本地测试台压测(不依赖比赛平台)

```bash
python3 tests/mock_contest_server.py --port 8899     # 与官方同构的本地接口(真 flag 校验 + 耗时报表)
./tests/local_contest.sh start|status|stop           # 一键:起 mock + 在 pwn64 起 pwn 服务 + 用 tests/localrun 跑 runner
python3 tests/scale_test.py 30                       # 调度回归(假题,不调用模型)
```
题源在 `tests/challenges.json`(字段:`id/title/category/description/flag/file/service/connection`)。
已实测:取证题(pcap)约 100 秒解出;PWN(ret2libc)在 2 agent 下曾 14 分钟未解出(根因见坑 4,已改 prompt)。

## 九、CTF 知识库(Agent 解题时随查随用)

用户明确要求:**agent 遇到不会的题要能方便地查 CTF 知识**(单靠 DeepSeek V4 不够)。
实现在 `knowledge/` + 检索工具 `tools/kb.py`,并已写进 worker prompt(含"卡住 8~10 步必须查"的强制自救流程):

```bash
python3 tools/kb.py list / search "关键词" / show <方向> / grep "常量" / reindex
```
三层设计保证知识真被用上:**① 把该方向 playbook 节选直接注入 prompt**(`solver.category_playbook`,约 2.6KB);
② 行动准则第 7 条的强制自救流程;③ runner 侧 Observer 在 worker 满 `KB_NUDGE_STEPS` 步未提交时,
往它必读的 `SHARED.md` 写"去查知识库"提醒(协同规则已改成"先读后写",确保提醒被看到)。
内容:六方向自写 playbook + `vendor/PayloadsAllTheThings`(Web payload 大全)+ `vendor/ctf-wiki`(中文系统知识)
+ `vendor/RsaCtfTool`(可直接跑)。索引自动缓存(`knowledge/.kb_index.json`),8949 段 0.2s。
**扩库方式**:新套路直接追加到 `knowledge/<方向>.md`;新资料放进 `knowledge/vendor/` 后跑一次 `kb.py reindex` 即可被检索到。
交付打包用 `--with-knowledge` 一并带上。

## 十、并发与限流(现场给到 50 并发)

- 默认 `START_WORKERS=24`(约一半额度,不贴 50)、`MIN_WORKERS=4`、`WORKERS_PER_QUESTION=2`;
  想更激进可在网页控制台把「起始并发」调到 30~36。
- 限流降级是**按比例**的:并发 ≥10 时每步退 20%,冷却 30s;24→4 约 5 分钟到底。
  识别到额度耗尽(quota)会整体暂停 `QUOTA_BACKOFF` 秒而不是空转。
- 靶场压测脚本(`tests/arena/run_arena.sh`)固定 `START_WORKERS=2` —— 因为 NSSCTF Arena **每个 Agent 同时只能有一道题**,
  并发只能体现在"同题 2 个 agent",与比赛的多题并行不是一回事。

## 十一、时间预算(每轮只有 30 分钟,速度第一)

- `.env` 里 `ROUND_WINDOW_MINUTES=30` + `STOP_SPAWN_BEFORE_END=120`:到点杀 worker、最后 2 分钟不再派新题;
  runner 状态行显示 `剩余 X分Y秒`。设 0 则不限时(平时压测用)。
- 单题限时 `WORKER_TIMEOUT=420`(7 分钟)、`MAX_ATTEMPTS=4`、`FAILED_RETRY_AFTER=120`、`POLL_INTERVAL=3` —— 都为短窗口调过。
- prompt 每次下发都带**时间预算**(剩余分钟 + 策略:最短路径 / 过半未破换面 / 剩 3 分钟先提交最有把握的候选)。
- 改窗口大小只需改 `ROUND_WINDOW_MINUTES`(控制台里改 .env 也行)。

## 十二、健壮性与测试(2026-09-13 加固)

- `./start.sh --supervise`:崩溃自动重启(runner 异常退出 → 5 秒后重来;正常 exit 0 不重启)。
- 心跳:`logs/runner_state.json.heartbeat`,看板 >60s 无心跳标红「runner 无响应」。
- 目录自愈 + 孤儿清理 + 窗口兜底(见 .env 的 ROUND_WINDOW_MINUTES)。
- 测试:`tests/unit_test.py`(28 项离线断言)、`tests/scale_test.py N`(调度回归)、
  `tests/local_contest.sh`(小规模端到端)、`tests/load/run_load.sh 24`(高并发压测)。
- 实测容量:24 并发 → 峰值 26 pi 进程 / 4GB 内存 / runner CPU<1%;12 道取证题平均 90s 解出、0 错误提交。

## 十三、长题(pwn)经验(2026-09-13 实测)

- 一道 pwn(i386/amd64 ret2libc)**约需 14~15 分钟墙钟**(2 agent × 2 轮),30 分钟窗口够但要占一半。
- 三条关键机制保证长题能做完:
  1. **重试保留工作目录**(agent 的 exp.py/笔记不丢,下轮接着改) + prompt 告知"第 N 次尝试,先看上次的脚本";
  2. **限时递增** `TIMEOUT_ESCALATE=120`(420→540→660→780s),后轮给更多时间;
  3. 同题两个 slot 的**工作区准备按题加锁**(否则并发下载/解压会互相删文件,报 FileNotFoundError)。
- 自建 pwn 服务务必用 `stdbuf -o0` 而不是 `pty`:`pty` 会回显输入 + 行缓冲,和真实容器行为差很多,agent 会误判协议。

## 十四、最终验收(2026-09-13,5 类全覆盖,走网页流程)

- 题集:`tests/final/challenges.json`(web 路径穿越 / pwn ret2libc / reverse XOR / crypto 小公钥指数 / forensics pcap);
  起环境 `./tests/final/run_final.sh start`(只起题目服务与 mock 接口,runner 请在网页上点「开始跑」)。
- **结果:5/5 全解、提交全对、约 3 分钟、零人工干预**;首轮退出后自动重试,重试时自动注入知识库片段(约 1.3~1.4KB/题)并解出。
- 网页全流程实测可用:填配置 → 保存 → 测试接口 → 测试模型 → 新建项目 → 开始跑 → 停止/重启。
- 明天现场:8799 已恢复比赛配置,只需在控制台填现场模型(Base URL/key/模型名/API 类型)→ 测试模型 → 开始跑。

## 十五、重试与时间边界(2026-09-13 用户定稿)

- **不设重试次数上限**(`MAX_ATTEMPTS=0`),**以时间为界**:从点「开始跑」起,窗口内坏了立刻重试(秒退等 5 秒)。
- 结束有两种设法:`ROUND_WINDOW_MINUTES=60`(时长)或 `ROUND_END_AT=14:00`(绝对时间,优先)。
  最后 `STOP_SPAWN_BEFORE_END=120` 秒停止派新题;到点杀所有 worker 并退出,之后不再跟进。
- **截止时间持久化**:重启(supervise/控制台「重启」)沿用同一 deadline,不会重新计时。
- 单题限时递增并封顶:`WORKER_TIMEOUT=420` + `TIMEOUT_ESCALATE=120` → 最高 `MAX_WORKER_TIMEOUT=900`。
- 实测(3 分钟窗口 + 故意打不通的 pwn 服务):窗口内持续自动重试、无"尝试用尽";到点前 20 秒停派新题、
  到点杀全部 worker、0 残留进程。

## 十六、最终版(2026-09-13 定稿)

- 模型:`deepseek-flash`(`https://api.deepseek.com/v1`,provider `rotom`)。
- 启动:`./start.sh`(默认崩溃自动重启)或双击 `启动Agent.command`;`--check-only/status/stop/--practice/--project/--workers/--no-supervise`。
- 计时:从点「开始跑」起 `ROUND_WINDOW_MINUTES=60`,或 `ROUND_END_AT=HH:MM`(优先);
  截止时间持久化、重启不重置、**过期即重新计时**(隔天启动不会卡死)。
- 停机:`./start.sh stop` 会同时收掉 supervisor 与所有 agent(0 残留进程)。

## 十七、PWN 效率优化(2026-09-13 诊断)

- 阶段拆解显示浪费主要在**导航开销**(每条命令写超长绝对路径,占 28%)和**不会用已有的 libc-database**。
- 已修:`knowledge/pwn.md` 增"libc 识别与本地复现(离线)"章节;prompt 对 pwn/reverse 追加"环境速查"
  (pwn64 与 macOS 共享文件系统、全新 shell 的路径写法、`cd ~/libc-database && ./find puts <低3位>`、禁止在线识别)。
- 效果:导航开销 28%→4.9%,打远程 6.5%→26.8%,单题 856~929s(4 次尝试)→**262s(1 次尝试)**。
- 排查要点:若再出现类似"某方向不顺",先做**阶段占比统计**(recon/local/remote/写脚本/导航),别猜。

## 十八、代理与熔断(2026-09-13 靶场复测后加固)

- **模型端点永远绕过本机代理**:`solver._provider_hosts()` 从 `~/.pi/agent/models.json` 读当前 provider 的 baseUrl 主机,
  加进 `no_proxy`;否则用户代理一失效(7877 无监听)模型请求就全挂(`Connection error.`)。
  `NETWORK_MODE=local_only` 时才额外把其它出网指向黑洞 `127.0.0.1:9`。
- `NETWORK_MODE`/`NET_ALLOW` 必须经 `env_config()` 传进 worker(曾漏传导致断网模式静默失效)。
- **空跑熔断**:worker 0 工具调用即退出 → 累计退避(20s×n,上限 300s),连续 3 次写黑板警告;
  `Connection error/ECONNREFUSED/fetch failed` 归为 `net` 类,退避重试且不算题目失败。
- 排查口令:遇到"worker 起来了但什么都没干",先看 worker 日志尾部的 `errorMessage`,再看 `env_config` 是否漏键。

## 十九、格攻击与 Coppersmith(2026-09-13 补齐)

- pwn64 已装 **fpylll**(apt);**sage 装不了**。格攻击一律 `orb -m pwn64 bash -lc '...'`(macOS 本机无 fpylll,sympy 慢 2 倍)。
- 自研工具 `tools/crypto/coppersmith.py`(部分密钥泄露 / 小根),512 位 n + 150 位未知约 1 分钟;pwn64 里跑。
- 三个坑:首一化(乘 LC 模逆)、格第一族取 `i=0..m-1`(否则秩亏)、**别对格元素取模**(会算错)。
- 慢任务用 `nohup ... &` 后台跑并写盘,避免被单题限时打断。
- 靶场复测:6 题解出、rating 1432→1599(93.75%);pwn 样本 14 次调用/90 秒 vs 66 次调用(导航占 39%)两种画像。
