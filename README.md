# wqh_fromkimi —— 湾区杯 AI 智能体解题赛 自动解题框架

> **架构与运行逻辑详解(含架构图 / 时序图 / 状态机):[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**

参考 Cairn 的架构思想(黑板 + 隔离 worker + origin/goal 任务),为 iChunQiu
比赛接口定制的轻量实现。本机 `pi` CLI 作为解题 worker,模型当前配置为 **deepseek-flash**(`https://api.deepseek.com/v1`)。

## 最终版启动方式(比赛当天照这个做)

```bash
./start.sh                 # 一键:体检 → 起控制台 → 后台开跑(默认开启崩溃自动重启)
```
或者**双击 `启动Agent.command`**(等价,并自动打开浏览器)。

跑完打印本轮截止时间,例如:`本轮截止 22:00:34(剩余 59 分 56 秒);期间坏了会自动重试,不限次数,到点自动收工`。

| 命令 | 作用 |
|---|---|
| `./start.sh` | 一键启动(体检/控制台/开跑,默认 `--supervise` 崩溃自动重启) |
| `./start.sh --check-only` | 只体检,不启动 |
| `./start.sh status` / `./start.sh stop` | 看状态 / 优雅停机(连 supervisor 与所有 agent 一起收) |
| `./start.sh --practice` | 练习模式(平台已解出的题也重跑) |
| `./start.sh --project X` / `--workers N` | 指定项目 / 覆盖并发 |
| `./start.sh --no-supervise` | 关闭崩溃自动重启 |

**当前模型配置**:`PI_BASE_URL=https://api.deepseek.com/v1`、`PI_MODEL=deepseek-flash`、`PI_API_TYPE=openai-completions`
(现场若换模型,在网页控制台改这四项 → 保存 → 测试模型)。

**本轮计时**:从点「开始跑」起算,`ROUND_WINDOW_MINUTES=60` 或用绝对时间 `ROUND_END_AT=14:00`;
截止时间会持久化,**重启不重置**,但**已过期则视为新一轮重新计时**(所以隔天再启动不会卡住)。

## 重试与时间边界(以时间为界,不设次数上限)

- **从点「开始跑」起计时**,窗口内**无限重试**:worker 超时/崩溃/退出都立刻重排(秒退等 5 秒,正常退出立即重试),
  不存在"重试 N 次就放弃"。
- **到点收工**:`ROUND_WINDOW_MINUTES=60`(或直接填绝对时间 `ROUND_END_AT=14:00`),最后 2 分钟(`STOP_SPAWN_BEFORE_END`)
  不再派新题,到点杀掉所有 worker 并退出 —— 之后的问题不再跟进。
- **截止时间持久化**在数据目录(`logs/run_deadline.json`):runner 崩溃被 `--supervise` 拉起、或在控制台点「重启」,
  都沿用同一个截止点,不会因为重启而重新计时。
- **单题限时逐轮递增且有上限**:420 → 540 → 660 → 780 → 900s(`TIMEOUT_ESCALATE=120`,`MAX_WORKER_TIMEOUT=900`),
  防止一道题吃掉整轮。
- 模型侧限制不消耗重试(本来也没有次数上限):429 降并发退避,额度耗尽整体暂停 `QUOTA_BACKOFF`。

## 运行策略(全自动,一键后不碰电脑)

1. **高并发抢跑**:`START_WORKERS` 个 worker 并行(默认 6),优先铺满不同题目;还有空位时同题可再加 agent(默认每题最多 2 个)。
2. **同题多 agent 靠共享黑板协同**:`work/<qid>/SHARED.md` 是所有 agent 只写追加的公共事实板,后启动的 agent 会先读它,避免重复劳动,也能沿别人的突破点加速。
3. **限流自动降并发**:worker 日志命中 429/rate limit/concurrency 等特征时,并发上限自动 -1(带 60 秒冷却,降到 `MIN_WORKERS` 为止),被限流杀掉的 worker 不消耗题目重试次数。
4. **解出即提交并继续**:agent 自己调用提交工具,平台确认后杀掉该题所有 worker,继续推其它题;全部处理完仍驻留轮询等新题(KEEP_RUNNING=1)。
5. **尝试用尽的题会自动复活**:`FAILED_RETRY_AFTER` 秒后清零重试次数重新挑战,窗口内持续努力。

## 架构

```
                 ┌────────────────────────────────────┐
                 │  runner.py (dispatcher, 主循环)     │
                 │  轮询题目 → 派题 → 回收/重试/对账     │
                 └───────┬──────────────┬─────────────┘
                         │ 每题一个 pi 进程(隔离 work/)
              ┌──────────┴──┐        ┌──┴──────────┐
              │ solver: misc│        │ solver: pwn │  ...
              │ pi + bash   │        │ pi + nc/gdb │
              └──────────┬──┘        └──┬──────────┘
                         │  拿到 flag 调 tools/submit_flag.py
                 ┌───────┴──────────────┴─────────────┐
                 │  board.json (黑板: 状态/facts/对账)  │
                 └────────────────────────────────────┘
```

- `contest_api.py` — 比赛三个接口(查题/重置/提交)的封装
- `board.py` — 黑板,`board.json` 持久化,flock 并发安全;题目状态机 pending→running→solved/failed
- `solver.py` — 每题独立 `work/<qid>_<title>/` 目录,自动下载并解压附件,生成 prompt,启动 pi
- `runner.py` — 主循环:轮询(默认 5s)、并行度(默认 3)、单题限时(默认 1500s)、失败重试(默认 2 次)、平台 is_solved 对账
- `tools/submit_flag.py` — agent 用 bash 调用提交 flag,正确才 exit 0,记录 submissions.log
- `tools/reset_env.py` — 容器题环境重置
- `dashboard.py` — 本地看板(标准库 HTTP 服务),实时展示各 agent 在做什么
- `tools/monitor.py` — 终端版监控,适合没有浏览器时用

## 项目(一次运行 = 一个项目,互不干扰)

每个项目有**自己独立的数据目录**:独立的题目状态(`board.json`)、工作区(`work/`)、日志(`logs/`)
和独立的 runner 进程。所以你可以同时开多个项目对照跑,不会互相覆盖或抢题。

- **默认项目**:就是代码根目录(向后兼容,命令行 `./run.sh` 直接跑也用它)
- **新建项目**:在总览页填名字点「创建项目」,数据落在 `projects/<项目名>/`
- 总览页选好项目后点 **「开始跑」**,它就用该项目自己的工作区去拉题、解题

```
wqh_fromkimi/
  board.json  work/  logs/      # 默认项目
  projects/
    round1/                     # 新建的项目
      board.json  work/  logs/  meta.json
    round2/
```

模型、token、比赛接口在「控制台」里统一配置,**所有项目共用**;
并发/限时等项目级参数目前也共用同一份 `.env`。

## 控制台(网页里改配置 + 启停)

```bash
./dashboard.sh                 # 默认 http://127.0.0.1:8799(端口被占用会自动往后试,启动时会打印实际地址)
```

**控制面板**里按三块组织,全部可直接编辑:

- **比赛接口**:队伍 token、接口域名 Base、查题 / 重置环境 / 提交 flag 三条路径(也支持整条 URL)
- **模型配置**:Base URL、API Key、模型名、API 类型(下拉,含 `anthropic-messages`)
- **运行参数**:起始并发、练习模式

按钮:「保存配置」「测试接口」「测试模型」「启动 runner / 停止 / 重启」。保存后会写入 `.env`、
同步成 pi 的 provider,所以比赛现场**全程不用碰命令行**。

- 「测试接口」= 用当前 token 和地址实拉一次题目,直接告诉你是"拉到 N 道题"还是具体报错(域名解析失败等);
- 「测试模型」= 用 pi 实跑一次,确认现场模型真的通;
- 「停止」会优雅收掉所有 agent(不留孤儿进程),「重启」= 停止 + 启动;
- 模型/token 改动需要重启 runner 才生效,页面会提示。

**看板**部分实时展示:每道题的状态徽章(待派发/运行中/已中断/已解出/已放弃)、各 agent 的**实时工具调用与发言**、
token 消耗、静默时长(超 180s 标记"可能卡住")、共享黑板、提交记录、事件流。
数据直接读 `board.json` / `logs/*.jsonl`,零依赖(只用 Python 标准库),离线可用,只监听 127.0.0.1。

> 关于选型:没有用 Langflow —— 它是"搭 LLM 工作流"的工具,需要额外起一套环境(Docker/Node),
> 而且它展示的是流程图,不是 agent 的实时运行状态。这个看板直接长在 harness 上,启动即用。
> 如果想要跨会话的 LLM 调用追踪/成本分析,可以考虑 Langfuse 或 Arize Phoenix(偏重、要 Docker),
> 但对"比赛时看一眼 agent 在干嘛"这个需求来说,内置看板更合适。

## 配置(两个入口,改完即生效)

**1. 改队伍 token 和比赛接口地址**:

```bash
python3 config.py --token icqXXXXXXXXXXXXXXXX
python3 config.py --contest-base https://apiterminator.ichunqiu.com \
                  --contest-query-path /04cb510e425bd8f64fa97ba66f3935e1 \
                  --contest-reset-path /deed3dba39e57b7cf95ea63ddd84e0c8 \
                  --contest-submit-path /ff874ef3172cbf4fd6ec2c5653a568e2
```

**推荐直接在做网页控制台里改**(见下),改完点「测试接口」立刻能验证。地址的填法有两种:
路径填相对值(拼在 Base 后面),或者把主办方给的完整 URL 整条粘进对应字段(以 http 开头时原样使用)。
四个字段都留空就用内置默认值。

**2. 改模型(base URL / key / 模型名 / API 类型)** —— 现场换主办方模型只需这一条:

```bash
python3 config.py --base-url https://api.xxx/v1 --key sk-xxx \
                  --model deepseek-v4-flash --api-type openai-completions
python3 config.py --test        # 用 pi 实跑一次,确认模型连通
python3 config.py               # 查看当前配置(key 打码显示)
```

`--api-type` 四选一(对应 pi 的 provider api 类型):

| API 类型 | 适用 |
|---|---|
| `openai-completions` | OpenAI Chat Completions,**最通用**,中转站/国产模型基本都选这个 |
| `anthropic-messages` | Anthropic Messages API(Claude,或中转的 anthropic 入口) |
| `openai-responses` | OpenAI Responses API |
| `google-generative-ai` | Google Generative AI |

也可以直接编辑 `.env` 里的 `PI_BASE_URL` / `PI_API_KEY` / `PI_MODEL` / `PI_API_TYPE` 四行——
harness 启动时会自动把这份配置同步成 pi 的 provider(名为 `wqh`),无需手工改 pi 的 models.json。

> **关于代理**:本机若开着代理(如 127.0.0.1:7877),模型流量会走代理——这是需要的,
> 比如 micuapi 直连会被重置、必须走代理。而**比赛接口域名(`*.ichunqiu.com`)始终直连**,
> 因为本机代理会破坏它的 HTTPS(实测 SSL_ERROR_SYSCALL)。到了比赛现场如果没有代理,
> 环境里就没有代理变量,模型流量自然直连,不需要改任何配置。

## 工作区结构

每道题一个独立目录,命名规则是 **`题目大类-题目名字`**(例如 `web-web01`):

```
work/
  web-web01/          # 题目工作区
    .qid              # 题目 ID 标记(防止同名题混淆)
    SHARED.md         # 同题多 agent 共享黑板
    files/            # 附件解压(所有 agent 只读引用)
    w1/  w2/          # 各 agent 的独立工作目录
  misc-misc01/
  pwn-pwn01/
```

## CTF 知识库(Agent 解题时随查随用,离线)

现场无外网,所以知识库全部落盘在 `knowledge/`,并提供了**检索工具**给 agent 直接调用:

```bash
python3 tools/kb.py list                          # 看知识库有什么
python3 tools/kb.py search "sql 注入 绕过"        # 关键词检索(中英文、多词=都要命中)
python3 tools/kb.py show pwn                      # 看某方向 playbook 全文
python3 tools/kb.py show vendor/ctf-wiki/docs/zh/docs/web/sqli.md
python3 tools/kb.py grep "0x67452301"             # 原样搜常量/特征串
```

- 索引:首次检索自动建立并缓存(`knowledge/.kb_index.json`),源文件变化自动重建;实测 8949 段仅需 0.2s。
- 内容:`knowledge/{web,pwn,reverse,crypto,forensics,misc}.md`(自写 playbook:打法、坑、提速要点)
  + `vendor/PayloadsAllTheThings`(Web payload 大全,142 篇)+ `vendor/ctf-wiki`(中文系统知识,776 篇)
  + `vendor/RsaCtfTool`(可直接跑的 RSA 自动攻击)。
- 已写进 worker prompt:遇到不会的先检索;连续 8~10 步无进展时**强制**查知识库对照常规套路、把题目 hint 丢进去检索,
  再换攻击面(见 solver.py 行动准则第 7 条)。
- 打包交付时用 `--with-knowledge` 可把知识库一并放进部署包。

## 使用(推荐:一键启动)

```bash
./start.sh                 # 体检 → 起网页控制台 → 后台自动开跑(比赛当天用这条)
./start.sh --practice      # 练习模式(平台已解出的题也重跑)
./start.sh --project round1 --workers 8    # 指定项目 / 覆盖起始并发
./start.sh --foreground    # 前台跑,直接看滚动日志(Ctrl+C 停止)
./start.sh --check-only    # 只体检:token、模型配置、接口连通性、工作区
./start.sh status | stop   # 看状态 / 优雅停机(连所有 agent 一起收)
```

旧的等价方式仍然可用:

```bash
./run.sh            # 全自动解题(加载 .env)
./run.sh --once     # 干跑:只拉题目看状态,不启动 worker
python3 tools/monitor.py   # 终端里的实时监控(不依赖浏览器)
```

运行中观察:
- 终端每 5 秒打印一次进度
- `tail -f logs/<qid>_*.jsonl` 看某题 pi 的实时推理
- `cat board.json` 看全局状态;`cat logs/submissions.log` 看提交记录
- `Ctrl+C` 会优雅杀掉所有 worker

## 决赛日切换模型

现场统一发 DeepSeek-V4-Flash 和 key。修改 `.env`:

```bash
PI_PROVIDER=<按现场给的接口类型>
PI_MODEL=deepseek-v4-flash
KIMI_API_KEY=<换成现场发的 key>   # 若走 OpenAI 兼容接口,改用 OPENAI_API_KEY 及自定义 provider
```

pi 支持自定义 provider(参考 `搭建参考/BreachWeave-main/docs/pi-docs/custom-provider.md`),
如需自定义 base_url,在 `~/.pi/agent/models.json` 中配置后即可用 `--provider` 指定。

## 题目很多时(比赛一场可能几十道)

调度器是按"广度优先"设计的,不会把并发压在少数几道题上:

- **先铺题再加深**:有 N 道题、并发上限 M 时,先给 M 道题各派 1 个 agent(尽量多覆盖),
  只有当题目数少于槽位数时,才给同一道题加派第二个 agent(`WORKERS_PER_QUESTION`);
- **限定并发**:在跑的 agent 数永不超过 `START_WORKERS`(或限流后降到的值),题多只会排队;
- **排队公平**:新题优先、尝试次数少的题优先,不会出现某题被饿死;
- **附件下载并行化**:下载/解压走线程池,题目多、附件大也不会卡住调度(超时 300s 释放槽位);
- **尝试次数只在真正启动时才计**:准备失败、进程崩溃都不会白烧重试次数;
- **日志自动清理**:`logs/` 超过 `MAX_LOG_MB`(默认 2GB)自动删最旧的 worker 日志。

题多时唯一需要你调的是**单题限时**:并发有限,一道题占着槽位 25 分钟会拖慢整体覆盖进度。
启动时如果题目数 > 并发上限 × 2,runner 会打印提示;建议把 `WORKER_TIMEOUT` 调低(如 600s),
让排队的题也能轮上,靠 `FAILED_RETRY_AFTER` 让没做出来的题过后再来。

回归测试(不调用模型、不联网,用假题验证以上行为):

```bash
python3 tests/scale_test.py 30     # 也可试 24 / 60 / 100
```

## 本机工具依赖

pi 解题时会用 bash 调用各类 CTF 工具。macOS 自带 python3/curl/nc 可用;
建议另行安装(见对话中的清单):pwntools、gdb、binwalk 等。

## 交付与审计(新版规则硬性要求)

决赛手册第七条:挑战轮次结束后工作人员统一收取智能体做**复测与过程审计**,需提交 `.zip`/`.tar.gz`
部署包,根目录带 README 说明"运行环境与依赖 / 配置与启动指南 / 日志与审计路径"。

本项目已内置对应能力:

```bash
python3 tools/export_trace.py         # 把 logs/*.jsonl 转成可读的 Thought/Action/Observation 轨迹 -> traces/
python3 tools/package_submission.py   # 一键打包 dist/wqh-agent-<项目>-<时间>.tar.gz(含 README 与轨迹)
python3 tools/package_submission.py --zip            # 追加 .zip 格式
```

- 轨迹导出把机器格式的 jsonl 转成人类可读报告(`traces/<题目>_w<槽位>.md` + `SUMMARY.md`),
  审计对应关系:工具调用=`tool_execution_start`(Action)、工具输出=`toolResult`(Observation)、
  模型发言=`message_end`(Thought)。
- **日志默认不再自动清理**(`MAX_LOG_MB=0`),因为审计需要完整轨迹;磁盘紧张时再设 `MAX_LOG_MB`,
  但请先跑一次 `export_trace.py` 把轨迹落盘。
- 打包会自动脱敏(密钥/token 换成占位符),只带 `.env.example`。

## 知识库是怎么被真正用上的(实测数据 + 四层投喂)

担心"知识库没人查等于没有",所以做了四层,越往后越不依赖模型自觉:

1. **方向 playbook 直接注入 prompt**(每次下发都带该方向的方法/坑/提速要点)—— 常见题不用查就已拥有;
2. **agent 自己检索**:`tools/kb.py search/show/grep`(8949 段索引 0.2s);
3. **卡住时强制查**:prompt 行动准则第 7 条(连续 8~10 步无进展必须查库)+ runner 侧 Observer 往它必读的
   `SHARED.md` 写提醒(协同规则是"先读后写",提醒必被看到);
4. **重试时 harness 主动投喂**(2026-09-13 新增):同一题第 2 次尝试起,runner 自动用题目关键词
   (`kb.py search "<类别> <标题> <描述>"`)检索并把 top 片段注入 prompt,标题注明"上一轮没解出,这是最相关的片段"。
   实测:第 1 次尝试不注入(先自己干)、第 2 次注入 1149 字。

**实测使用率**(统计真实运行日志里的 `kb.py` 调用):
| 场景 | agent 会话数 | 查库次数 | 什么情况下查的 |
|---|---|---|---|
| NSSCTF 靶场(陌生题多) | 10 | **18** | Go FileServer 越界绕过、ASPack 加壳脱壳、unicorn 模拟脱壳 —— 都是 playbook 没覆盖的长尾 |
| 本地压测(取证/pwn) | 8 | 0 | 因为该方向 playbook 已直接注入 prompt,不需要额外查 |

结论:agent 在**遇到陌生题型时会主动查**,常见题则直接用注入的 playbook;现在再加上"重试自动投喂"兜底,
知识库不再依赖模型想起来去查。压测里它们查过而库里没有的内容(Go 静态文件服务技巧、加壳脱壳流程)也已补进
`knowledge/web.md` 和 `knowledge/reverse.md`。

## 压测与回归(四套,都不依赖比赛平台)

| 脚本 | 作用 | 是否耗模型额度 |
|---|---|---|
| `python3 tests/unit_test.py` | 28 项离线断言:挑战窗口、并发降级步长、靶场映射、提交判定、黑板状态机、prompt 组装 | 否 |
| `python3 tests/scale_test.py 30` | 调度回归:广度优先、并发上限、无题饿死、尝试次数不泄漏(24/30/60/100 题) | 否 |
| `./tests/local_contest.sh start` | 本地端到端:同构 mock 接口 + pwn 服务 + 真跑 agent(小规模) | 是 |
| `./tests/load/run_load.sh 24` | **高并发压测**:14 道题 × 24 并发,带容量与耗时报表 | 是 |

### 实测容量(2026-09-13,M3 Mac / DeepSeek Flash / 24 并发)
- **12 道取证题全部解出,平均 90 秒(48~138s),每题只提交 1 次、0 次错误提交**;2 道 pwn 副本自动进入长时求解。
- 峰值 **26 个 pi 进程 / 4.0GB 内存**,runner 自身 CPU <1% —— 瓶颈在模型与题本身,不在 harness。
- 全程 **runner 异常 0 行**;加载 24 并发时 mock/查题接口无异常。

## 运行期健壮性

- **崩溃自动重启**:`./start.sh --supervise`(runner 异常退出等 5 秒重来;正常收工 exit 0 不再重启)。
- **心跳监控**:runner 每轮写 `logs/runner_state.json` 的 `heartbeat`;看板在超过 60 秒无心跳时标红「runner 无响应」。
- **窗口兜底**:到 `ROUND_WINDOW_MINUTES` 自动杀 worker 停派新题;最后 2 分钟不再接新题。
- **孤儿清理**:启动时按进程名校验后清理上次残留的 pi 进程,防偷跑烧额度。
- **审计日志不自动清理**(默认 `MAX_LOG_MB=0`),超过 2GB 会在日志里提示手动归档。
- **目录自愈**:数据目录缺失时 `board.locked()` 与 runner 启动会自动创建。
- **赛前体检**:`./start.sh --check-only` 逐项检查 python/pi/tshark/orb/知识库/磁盘/接口连通性。
