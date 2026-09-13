---
name: wanqu-ctf-agent
description: 操作 2026 湾区杯 AI 智能体解题赛(环节二/三)的自研 harness。适用于:启动或停止这个自动解题项目、改队伍 token 或模型配置、排查 runner/agent 不跑或跑得慢、看实时状态、给题目加知识库经验、按新版规定导出审计轨迹或打包交付、赛前用本地模拟比赛做压测。关键词:wqh_fromkimi、湾区杯、智能体解题赛、start.sh、dashboard、runner、SHARED.md、审计打包。
---

# 湾区杯 AI 智能体解题赛 —— harness 操作手册

项目位置固定为 `/Users/jerry/CTFmac/比赛/26湾区决赛/agent/wqh_fromkimi`(下称项目根)。
比赛形式:**按一次「开始」后全程不再碰电脑**,agent 自动拉题→解题→提交;现场提供模型 key,
现场**无外网**,所有参考资料必须已落盘。

## 一、最常用操作

```bash
cd /Users/jerry/CTFmac/比赛/26湾区决赛/agent/wqh_fromkimi
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
- 配置全部落在 `.env`;**模型配置会自动同步成 pi 的 provider `wqh`**(不用手改 models.json)。
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
python3 tools/package_submission.py  # → dist/wqh-agent-<项目>-<时间>.tar.gz(含合规 README、脱敏 .env.example、traces)
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
