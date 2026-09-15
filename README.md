# Rotom ⚡

> 「它会用由离子构成的身体潜入各种各样的机器里,最喜欢将其作为躯体。」
> —— [神奇宝贝百科 · 洛托姆](https://wiki.52poke.com/wiki/%E6%B4%9B%E6%89%98%E5%A7%86)

**Rotom**(洛托姆,全国图鉴 No.479)是电 + 幽灵属性的「等离子宝可梦」,名字是 **MOTOR 的倒写**。
它能潜入不同的机器、随宿主变换形态——烤箱、洗衣机、冰箱、风扇、割草机,五种形态,属性与招式跟着换;
潜入图鉴之后,它就成了会说话、会辅助训练家的 **洛托姆图鉴**。

把这个名字给一个自动化 CTF 答题 Agent 再合适不过:**同一副骨架,潜入哪类题,就长成解决那类题的形状**。
Web / Pwn / Reverse / Crypto / Forensics 各有一套 playbook 与武器库,而调度、并发、协同、审计、看护这些骨架完全共用;
它也像洛托姆图鉴一样,全程自动干活,并且把每一步都实时讲给你听。

> 架构与运行逻辑详解(架构图 / 时序图 / 状态机):[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## 它是什么

参考 [Cairn](https://github.com/oritera/Cairn) 的架构思想(黑板 + 隔离 worker + origin/goal 任务)、为「AI 智能体解题赛」类比赛定制的轻量 harness:

- **调度层不碰题**:轮询题目、广度优先派发、并发控制、超时重试、平台对账;
- **解题层**每题一个隔离的 agent 进程(本机 `pi` CLI),自带独立工作区、附件、共享黑板与方向知识库;
- **提交与记账**统一走 harness 工具,完整审计轨迹可导出。

模型不绑定:任何 OpenAI 兼容 / Anthropic / Google 端点都能接,比赛现场在网页控制台填 Base URL + Key 即可切换,全程不碰命令行。

## 实战战绩

| 场景                                                 | 结果                                                 |
| -------------------------------------------------- | -------------------------------------------------- |
| 正式赛第二轮(10 题:web×3/pwn×2/reverse×2/misc×2/crypto×1) | **10/10 全解,总耗时 10 分 09 秒**,起跑 18 秒拿第一血,提交 10/10 全对 |
| 正式赛第三轮(巅峰对决,题目难度显著上升)                              | **6/10**,0 次错误提交;未解出的 4 道均为长链路 web/heap 题          |
| 本地 14 题 × 36 并发压测                                  | **14/14 全解**,平均 67s(最快 19s),0 次限流降级                |
| 本地 5 类验收(web/pwn/reverse/crypto/forensics)         | **5/5**,约 3 分钟,提交成功率 100%                          |
| NSSCTF Agent Arena 靶场(60 分钟窗口)                     | 单轮 12/13,累计 **21/22 = 95.45%**,rating 1432 → 1759  |

## 快速开始(比赛当天就这两步)

```bash
./start.sh                 # 一键:体检 → 起控制台 → 后台开跑(默认开启崩溃自动重启)
```

或者**双击 `启动Agent.command`**(等价,并自动打开浏览器)。

跑完会打印本轮截止时间,例如:`本轮截止 22:00:34(剩余 59 分 56 秒);期间坏了会自动重试,不限次数,到点自动收工`。

| 命令                                       | 作用                                      |
| ---------------------------------------- | --------------------------------------- |
| `./start.sh`                             | 一键启动(体检/控制台/开跑,默认 `--supervise` 崩溃自动重启) |
| `./start.sh --check-only`                | 只体检,不启动                                 |
| `./start.sh status` / `./start.sh stop`  | 看状态 / 优雅停机(连 supervisor 与所有 agent 一起收)  |
| `./start.sh --practice`                  | 练习模式(平台已解出的题也重跑)                        |
| `./start.sh --project X` / `--workers N` | 指定项目 / 覆盖并发                             |
| `./start.sh --no-supervise`              | 关闭崩溃自动重启                                |

**模型配置**:`.env` 里的 `PI_BASE_URL / PI_API_KEY / PI_MODEL / PI_API_TYPE` 四项(或在网页控制台改)。
现场换主办方模型 = 控制台改四项 → 保存 → 点「测试模型」。

**本轮计时**:从点「开始跑」起算,`ROUND_WINDOW_MINUTES=60`,或用绝对时间 `ROUND_END_AT=14:00`;
截止时间会持久化,**重启不重置**,但**已过期则视为新一轮重新计时**(所以隔天再启动不会卡住)。

## 它怎么解题(运行策略:全自动,一键后不碰电脑)

1. **高并发抢跑**:`START_WORKERS`(默认 36,按现场模型额度调)个 worker 并行,**广度优先**先铺满不同题目;
2. **同题多 agent 靠共享黑板协同**:`work/<题目>/SHARED.md` 是所有 agent 只写追加的公共事实板,后启动的 agent 先读它,避免重复劳动、能沿别人的突破点加速;
3. **限流自动降并发**:worker 日志命中限流特征时,并发上限**按 20%/步递减**(冷却 30s,下限 `MIN_WORKERS=4`),实测序列 36→29→24→20→16→13→11→9→…→4;被限流的 worker 不消耗题目重试机会;
4. **解出即提交并继续**:agent 调用提交工具,平台确认后杀掉该题全部 worker,继续推其它题;全部处理完仍驻留轮询等新题(`KEEP_RUNNING=1`);
5. **没解出的题持续复活**:窗口内以时间为界不断重试(见下),重试时自动注入知识库最相关片段。

## 并发与"按需增援"(agent 不能自己起进程,由 harness 统一派发)

- 默认**每题 2 个 agent**;agent 若觉得需要更多算力,**在共享黑板写一行请求**:
  `echo "- [wN 请求增援] 需要 2 个 agent:一个爆破 key 区间,一个扫 /admin" >> SHARED.md`
  runner 解析后,**仅在有空闲槽位时**给该题加派(上限 `MAX_HELPERS_PER_QUESTION=4`),
  并把请求里的分工原文作为新 worker 的专门任务注入其 prompt;同一请求只满足一次(黑板记账)。
- **不建议让 agent 自己 spawn 子进程**:会绕过并发控制(打爆模型额度)、绕过提交记账(黑板不更新)、审计轨迹散乱。需要并行时,harness 是唯一派发方。
- prompt 里同时教了"自己并行"的写法(爆破/扫描用 `xargs -P`、后台任务 + 限时),多数场景不需要额外 agent。

## 重试与时间边界(以时间为界,不设次数上限)

- **从点「开始跑」起计时**,窗口内**无限重试**:worker 超时/崩溃/退出都立刻重排(秒退等 5 秒),不存在"重试 N 次就放弃";
- **到点收工**:`ROUND_WINDOW_MINUTES` 到点杀掉所有 worker 并退出,最后 2 分钟(`STOP_SPAWN_BEFORE_END`)不再派新题;
- **截止时间持久化**在数据目录(`logs/run_deadline.json`):runner 崩溃被拉起、或控制台点「重启」,都沿用同一截止点,不会因重启而重新计时;
- **单题限时逐轮递增且有上限**:420 → 540 → 660 → 780 → 900s(`TIMEOUT_ESCALATE=120`,`MAX_WORKER_TIMEOUT=900`),防止一道题吃掉整轮;
- 模型侧限制不消耗重试:限流降并发退避;额度耗尽整体暂停 `QUOTA_BACKOFF` 秒。

## 架构

![Rotom 架构:runner 主循环派发 → 每题一个隔离 solver 进程 → 统一提交与黑板记账](docs/architecture.png)

- `contest_api.py` — 比赛三个接口(查题/重置/提交)的封装;提交内置**限流自动退避**,并区分「答案错误」与「平台未受理」
- `board.py` — 黑板,`board.json` 持久化,flock 并发安全;题目状态机 pending→running→solved/failed
- `solver.py` — 每题独立工作区,自动下载解压附件,组装 prompt(方向 playbook + 知识库片段 + 时间预算),启动 pi;
  另含失败分类器(超时/限流/额度/网络),分类时**强制错误语义上下文**,避免把模型自己的思考文本误判成服务端错误
- `runner.py` — 主循环:轮询(3s)、广度优先派发、同题默认 2 agent、单题限时 420s 起步递增、以时间为界无限重试、平台对账
- `tools/submit_flag.py` — agent 用 bash 调用提交;`CORRECT`/`WRONG`/`SUBMIT_BUSY`(限流,可重试不算答错)三种结论
- `tools/reset_env.py` — 容器题环境重置
- `dashboard.py` — 本地看板(纯标准库),实时展示各 agent 在做什么
- `tools/monitor.py` — 终端版监控,适合没有浏览器时用

## 多项目(一次运行 = 一个项目,互不干扰)

每个项目有**自己独立的数据目录**:独立的题目状态(`board.json`)、工作区(`work/`)、日志(`logs/`)和独立的 runner 进程。
可以同时开多个项目对照跑,不会互相覆盖或抢题。

- **默认项目**:就是代码根目录(向后兼容,命令行直接跑也用它)
- **新建项目**:总览页填名字点「创建项目」,数据落在 `projects/<项目名>/`

```
<项目根>/
  board.json  work/  logs/      # 默认项目
  projects/
    round1/                     # 新建的项目
      board.json  work/  logs/  meta.json
    round2/
```

模型、token、比赛接口在「控制台」统一配置,**所有项目共用**;并发/限时等项目级参数目前也共用同一份 `.env`。

## 网页控制台(改配置 + 启停 + 看盘)

```bash
./dashboard.sh                 # 默认 http://127.0.0.1:8799(端口被占用自动往后试,启动时打印实际地址)
```

**控制面板**三块,全部可直接编辑:

- **比赛接口**:队伍 token、接口域名 Base、查题 / 重置环境 / 提交 flag 三条路径(也支持整条 URL)
- **模型配置**:Base URL、API Key、模型名、API 类型(下拉,含 `anthropic-messages`)
- **运行参数**:起始并发、正式/练习模式、**网络模式(direct 直连 / system 走系统代理)**

按钮:「保存配置」「测试接口」「测试模型」「启动 runner / 停止 / 重启」。保存即写入 `.env` 并自动同步模型配置,比赛现场全程不用碰命令行。

- 「测试接口」= 用当前 token 和地址实拉一次题目,直接告诉你是"拉到 N 道题"还是具体报错;
- 「测试模型」= 实跑一次模型,确认现场端点真的通;
- 「停止」优雅收掉所有 agent(不留孤儿进程),「重启」= 停止 + 启动;模型/token 改动需重启 runner 才生效,页面会提示。

**看板**实时展示:每道题的状态徽章(待派发/运行中/已中断/已解出/已放弃)、各 agent 的**实时工具调用与发言**、token 消耗、
静默时长(超 180s 标"可能卡住")、共享黑板、提交记录、事件流。数据直接读 `board.json` / `logs/*.jsonl`,零依赖、离线可用、只监听 127.0.0.1。

> 关于选型:没有用 Langflow——它是"搭 LLM 工作流"的工具,要额外起一套环境(Docker/Node),展示的是流程图而非 agent 实时状态。
> 这个看板直接长在 harness 上,启动即用。若要跨会话的 LLM 调用追踪/成本分析,可考虑 Langfuse 或 Arize Phoenix(偏重、要 Docker)。

## 配置(两个入口,改完即生效)

**1. 队伍 token 和比赛接口地址**:

```bash
python3 config.py --token icqXXXXXXXXXXXXXXXX
python3 config.py --contest-base https://<主办方域名> \
                  --contest-query-path /query --contest-reset-path /reset --contest-submit-path /submit
```

推荐直接在网页控制台改,改完点「测试接口」立刻验证。路径可填相对值(拼在 Base 后面),也可把主办方给的完整 URL 整条粘进对应字段;留空用内置默认值。

**2. 模型(Base URL / Key / 模型名 / API 类型)** —— 现场换主办方模型只需这一条:

```bash
python3 config.py --base-url https://api.xxx/v1 --key sk-xxx \
                  --model deepseek-flash --api-type openai-completions
python3 config.py --test        # 实跑一次,确认模型连通
python3 config.py               # 查看当前配置(key 打码显示)
```

`--api-type` 四选一(对应 pi 的 provider api 类型):

| API 类型                 | 适用                                               |
| ---------------------- | ------------------------------------------------ |
| `openai-completions`   | OpenAI Chat Completions,**最通用**,中转站/国产模型基本都选这个   |
| `anthropic-messages`   | Anthropic Messages API(Claude,或中转的 anthropic 入口) |
| `openai-responses`     | OpenAI Responses API                             |
| `google-generative-ai` | Google Generative AI                             |

也可以直接编辑 `.env` 里 `PI_BASE_URL / PI_API_KEY / PI_MODEL / PI_API_TYPE` 四行——
harness 启动时会自动把这份配置写进 pi 的 models.json(生成一个专用 provider 条目),无需手工改。

> **关于网络(实测教训)**:`.env` 的 `NET_PROXY=direct`(默认)会让 harness **主动摘掉** http_proxy 等代理变量——
> 本机代理客户端一关(或现场没有代理),带着失效代理变量发请求会瞬间 `Connection error`,直连模式从根上免疫这个问题。
> 比赛接口域名始终直连(本机代理会破坏它的 HTTPS)。确实需要代理出网时改成 `NET_PROXY=system`。

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

**重试保留现场**:worker 超时被回收后,工作目录与黑板**不清空**,重试的新 agent 先读前人的笔记再动手——
实测两道硬题:第一次尝试跑满 420s 被限时回收,接手的 agent 读着笔记 **40s / 63s** 就解完了。

## CTF 知识库(Agent 解题时随查随用,离线)

现场无外网,知识库全部落盘在 `knowledge/`,并给 agent 提供检索工具:

```bash
python3 tools/kb.py list                          # 看知识库有什么
python3 tools/kb.py search "sql 注入 绕过"        # 关键词检索(中英文、多词=都要命中)
python3 tools/kb.py show pwn                      # 看某方向 playbook 全文
python3 tools/kb.py grep "0x67452301"             # 原样搜常量/特征串
```

- 索引:首次检索自动建立并缓存,源文件变化自动重建;9000+ 段检索仅 0.2~0.3s。
- 内容:`knowledge/{web,pwn,reverse,crypto,forensics,misc}.md`(自写 playbook:打法、坑、提速要点)
  + `vendor/PayloadsAllTheThings`(Web payload 大全)+ `vendor/ctf-wiki`(中文系统知识)+ `vendor/RsaCtfTool`(可直接跑的 RSA 自动攻击),
    合计 900+ 篇文档。
- 第三方资料**不入 git 仓库**,clone 后跑 `knowledge/fetch_vendor.sh` 一键重建(自动克隆三个仓库并重建索引)。

## 知识库是怎么被真正用上的(四层投喂,实测数据)

担心"知识库没人查等于没有",所以做了四层,越往后越不依赖模型自觉:

1. **方向 playbook 直接注入 prompt**(每次下发都带该方向的方法/坑/提速要点)——常见题不用查就已拥有;
2. **agent 自己检索**:`tools/kb.py search/show/grep`;
3. **卡住时强制查**:prompt 行动准则要求连续 8~10 步无进展必须查库对照常规套路,runner 侧 Observer 还会往它必读的 `SHARED.md` 写提醒;
4. **重试时 harness 主动投喂**:同一题第 2 次尝试起,自动用题目关键词检索并把 top 片段注入 prompt,注明"上一轮没解出,这是最相关的片段"。

**实测使用率**(统计真实运行日志里的 `kb.py` 调用):

| 场景           | agent 会话数 | 查库次数   | 什么情况下查的                                        |
| ------------ | --------- | ------ | ---------------------------------------------- |
| 靶场(陌生题多)     | 10        | **18** | 文件服务越界绕过、加壳脱壳、unicorn 模拟脱壳——都是 playbook 没覆盖的长尾 |
| 本地压测(取证/pwn) | 8         | 0      | 该方向 playbook 已直接注入 prompt,不需要额外查               |

结论:agent 在**遇到陌生题型时会主动查**,常见题直接用注入的 playbook;加上"重试自动投喂"兜底,知识库不再依赖模型想起来去查。
压测里它们查过而库里没有的内容,也已补回对应方向的 playbook。

## 比赛期看护与运维工具(实战沉淀)

| 工具                                         | 作用                                                                                            |
| ------------------------------------------ | --------------------------------------------------------------------------------------------- |
| `python3 tools/watchdog.py --project X`    | **常驻看护**:每 15s 体检;控制台挂了自动重拉、窗口内 runner 掉了自动重拉、心跳停滞 180s 判假死杀掉重拉;开赛前每 45s 探一次"是否提前放题";窗口到点自行退出 |
| `python3 tools/status_now.py --project X`  | 一屏状态快照:窗口剩余、并发、每题尝试次数、提交判定(CORRECT/WRONG/未被受理)、错误计数                                           |
| `python3 tools/round_watch.py --project X` | 事件哨兵:平时静默,**有新题出现 / runner 掉线 / 窗口到点**就退出并唤醒监工                                                |
| `python3 tools/postmortem.py --project X`  | **赛后复盘**:按会话统计工具调用/回合/token/上下文压缩/报错,把 bash 命令分类(纯导航、重复、上网尝试、找工具),定位"磕磕绊绊"在哪,不靠印象             |
| `tools/bin/timeout`                        | macOS 没有 GNU `timeout`,harness 自带替身并注入 worker 的 PATH,agent 才能给扫描/爆破类长命令限时                     |

**实战中当场修掉的两个 harness 缺陷**(为什么这套工具值得存在):

1. **失败分类被模型散文误伤**:分类器在 worker 日志全文上匹配关键词,而日志里混着模型自己的思考——
   crypto 题的 agent 写了句 "the modulus n is not **balanced**",被裸词 `balance` 命中,误判"额度耗尽"导致全局暂停 900 秒。
   修复:只匹配带错误语义的完整短语,且要求关键词附近同时出现错误标记(单测钉死)。
2. **无界长命令吃掉解题窗口**:macOS 缺 `timeout`,agent 写的 `timeout 60 nmap ...` 全部 `command not found`,
   放开跑的 `nmap -p-` 一挂就是 8 分钟。修复:内置 `timeout` 替身 + 提示词强制"长命令一律限时"。

## 题目很多时(比赛一场可能几十道)

调度器按"广度优先"设计,不会把并发压在少数几道题上:

- **先铺题再加深**:有 N 道题、并发上限 M 时,先给 M 道题各派 1 个 agent(尽量多覆盖),题目数少于槽位数时才给同题加派第二个;
- **排队公平**:新题优先、尝试次数少的题优先,不会出现某题被饿死;
- **附件下载并行化**:下载/解压走线程池,题目多、附件大也不会卡住调度(超时 300s 释放槽位);
- **尝试次数只在真正启动时才计**:准备失败、进程崩溃都不会白烧重试;
- **日志自动清理**:`logs/` 超 `MAX_LOG_MB` 自动删最旧(交付审计模式默认不清理,见下)。

题多时唯一需要调的是**单题限时**:题目数 > 并发上限 × 2 时 runner 会打印提示,建议调低 `WORKER_TIMEOUT`,靠重试让没做出来的题过后再来。

回归测试(不调用模型、不联网):

```bash
python3 tests/scale_test.py 30     # 也可试 24 / 60 / 100
```

## 本机工具依赖

- [pi CLI](https://github.com/badlogic/pi-mono)(模型接入层,worker 即 `pi --mode json` 非交互进程);
- Python 3.10+(纯标准库,无 pip 依赖);
- macOS 自带 python3/curl/nc 可用;解题工具建议安装:pwntools、gdb、binwalk、tshark 等,容器题可用 OrbStack Linux 虚拟机(`orb`);
- macOS 上 `timeout` 由 harness 自带(见上),无需 coreutils。

## 交付与审计(比赛硬性要求)

赛会规则:挑战结束后工作人员统一收取智能体做**复测与过程审计**,需提交部署包,根目录带 README 说明
"运行环境与依赖 / 配置与启动指南 / 日志与审计路径"。本项目内置对应能力:

```bash
python3 tools/export_trace.py         # 把 logs/*.jsonl 转成可读的 Thought/Action/Observation 轨迹 -> traces/
python3 tools/package_submission.py   # 一键打包部署包到 dist/(含 README 与轨迹,自动脱敏)
python3 tools/package_submission.py --zip            # 追加 .zip 格式
```

- 轨迹导出把机器格式的 jsonl 转成人类可读报告(`traces/<题目>_w<槽位>.md` + `SUMMARY.md`),
  审计对应关系:工具调用=`tool_execution_start`(Action)、工具输出=`toolResult`(Observation)、模型发言=`message_end`(Thought);
- **日志默认不自动清理**(`MAX_LOG_MB=0`),审计需要完整轨迹;磁盘紧张时再设 `MAX_LOG_MB`,但请先跑一次 `export_trace.py`;
- 打包会自动脱敏(密钥/token 换占位符),只带 `.env.example`。

## 压测与回归(四套,都不依赖比赛平台)

| 脚本                               | 作用                                                                  | 是否耗模型额度 |
| -------------------------------- | ------------------------------------------------------------------- | ------- |
| `python3 tests/unit_test.py`     | **58 项离线断言**:挑战窗口、并发降级步长、提交判定(限流≠答错)、失败分类上下文闸门、网络策略、黑板状态机、prompt 组装 | 否       |
| `python3 tests/scale_test.py 30` | 调度回归:广度优先、并发上限、无题饿死、尝试次数不泄漏(24/30/60/100 题)                         | 否       |
| `./tests/local_contest.sh start` | 本地端到端:同构 mock 接口 + pwn 服务 + 真跑 agent(小规模)                           | 是       |
| `./tests/load/run_load.sh 24`    | **高并发压测**:14 道题 × 24 并发,带容量与耗时报表                                    | 是       |

### 实测容量(M3 Mac / DeepSeek Flash / 24 并发)

- 12 道取证题全部解出,平均 90 秒(48~138s),每题只提交 1 次、**0 次错误提交**;2 道 pwn 自动进入长时求解;
- 峰值 **26 个 pi 进程 / 4.0GB 内存**,runner 自身 CPU <1% —— 瓶颈在模型与题本身,不在 harness;
- 全程 runner 异常 0 行。

## 运行期健壮性

- **崩溃自动重启**:`--supervise`(runner 异常退出等 5 秒重来;正常收工不再重启);
- **心跳监控**:runner 每轮写 `logs/runner_state.json` 的 `heartbeat`;看板超 60 秒无心跳标红「runner 无响应」;
- **窗口兜底**:到点自动杀 worker 停派新题;最后 2 分钟不再接新题;
- **孤儿清理**:启动时按进程名校验后清理上次残留的 agent 进程,防偷跑烧额度;
- **提交限流免疫**:平台并发限流(「操作太过频繁」)自动退避重试,且**不记为答错**、不消耗错误提交次数;
- **失败分类防误伤**:限流/额度/网络的判定要求"关键词 + 错误语义上下文"同时命中,模型散文不再触发全局动作;
- **目录自愈**:数据目录缺失时自动创建;**赛前体检**:`./start.sh --check-only` 逐项检查 python/pi/tshark/orb/知识库/磁盘/接口连通性。

## 致谢

- [Cairn](https://github.com/oritera/Cairn) —— 黑板协同与 origin/goal 任务的架构思想来源;
- [pi](https://github.com/earendil-works/pi) —— 模型接入层与 agent 运行时;
- [PayloadsAllTheThings](https://github.com/swisskyrepo/PayloadsAllTheThings) / [ctf-wiki](https://github.com/ctf-wiki/ctf-wiki) / [RsaCtfTool](https://github.com/RsaCtfTool/RsaCtfTool) —— 离线知识库与工具(经 `knowledge/fetch_vendor.sh` 重建,不随仓库分发);
- 洛托姆(Rotom)形象归属于 Nintendo / Game Freak / The Pokémon Company,本项目与其无任何隶属关系,仅向这只"潜入机器、随宿主变形"的电鬼致敬。
