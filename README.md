<div align="center">

<img src="docs/logo.png" alt="Rotom" width="300"/>

# Rotom

**通用 AI 解题智能体平台:CTF 比赛 · 漏洞挖掘(众测/TSec) · 客户授权渗透**

</div>

---

`Rotom` 是一个**可切换策略的 AI 解题智能体平台**:调度层、黑板、知识库、网页控制台、审计轨迹在三种场景下完全共用,
只把"宿主适配层"换成对应平台(`tsec_api.py` / `contest_api.py` / `arena_api.py` / `targets_api.py`)。
完整目录与文件用途见 [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md);版本演进见 git 提交历史。

> 本仓库即 Rotom 的完整开源版:CTF 解题内核 + 多策略包(众测/渗透/涉诈侦查/新榜单)。

## 五个策略包:CTF / 众测 / 正式渗透 / 涉诈侦查 / 新榜单

同一套机制,策略用数据切换(`PROFILE=ctf|zhongce|pentest|shezha|newrank`,网页控制台「创建项目」时也能选):

| 包 | 场景 | 完成定义 | 任务来源 | 容器 |
|---|---|---|---|---|
| `ctf` | CTF 解题赛 | 提交 `CORRECT` | 平台接口 | 平台托管 |
| `zhongce`(默认) | 众测/跑分平台 | `本题通关 x/x` | 平台接口 | harness 自管 + 名额轮换 |
| `pentest` | 客户授权渗透 | `report_finding --done` | 本地 `targets.json` | 委托方环境 |
| `shezha` | 涉诈网站侦查(全自动批次) | `--done` + 档案齐备闸门 | 本地 `targets.json`(Excel 榜单切片) | 无(走探测代理) |
| `newrank` | 2026 比武 · 新榜单 | 提交 `Correct` | 平台 `getquestion/submitflag` | 无(Web 直接入口) |

**新榜单(`newrank`)的阶段与附件**:阶段 1 是 Web 直接入口;阶段 2/3 是逆向/密码/取证/Web 源码审计,
题面只给「附件下载」链接(没有远程入口),适配器会把附件解析出来、由 solver 下载并解包到 `files/`,
并按题干推断方向(逆向/密码/取证/Web)注入对应 playbook。`NEWRANK_STAGE=1,2,3` 会按阶段增量合并题单,
缓存里已有阶段的票据/Cookie 不会被翻新。
**新榜单(`newrank`)的两个平台特性**:① 题单里的入口 URL 带**一次性** `__ctf_ticket`,首次访问返回 303 + `Set-Cookie`,
票据当场作废;适配器在拉题时就替 agent 兑换好会话 Cookie,并把 Netscape `cookies.txt` 写进每个 worker 的工作目录
(提示词明确要求 curl/dirsearch/sqlmap/ffuf/requests 全部带 Cookie)。② 平台提交响应没有 `correct_flag_count`,
适配器补成 1/1,提交工具才能把黑板标成 solved(否则 agent 会对着已解出的题继续烧)。
**AI 调用监控**:`tools/pi_extensions/ai_trace.ts` 记录每次模型调用的起止/首包/usage 到 `<worker日志>.calls`,
`tools/ai_monitor.py` 增量聚合成 `logs/ai_usage.json`;控制台顶栏「AI」显示总 token、token/分钟与平均延迟,
点开可按题目看明细。`ROTOM_AI_TRACE=0` 可关。

提示词/红线/工具链/playbook 都在 `prompts/<包>/` 与 `profiles/<包>.json` 里(改文案不用动代码);
每个包还能带一份 **pi 系统提示词**(`--append-system-prompt` 注入;`ctf`/`zhongce` 共用一份,正式渗透那份含授权声明)。
**策略包在网页控制台「创建项目」时选**,存在项目的 `meta.json` 里(项目优先于全局 `.env`);旧项目可在同一个卡片里删除。
正式渗透的产出是 `FINDINGS.md` / `findings.jsonl`,收工用 `python3 tools/pentest_report.py` 出 `REPORT.md`;
`shezha` 包的产出是站点档案(00_档案.md/证据/报告),worker 直接落在档案目录的 `工作区/`。
**涉诈控制台自带「成果评读」**:AI 通读每个站的档案判档——扎实 / 有获取 / **待人工审核** / 仅探测 / 无材料;
只拿到备案号·营业执照这类"证明站点正规"的材料**不算战果**,落「待人工审核」交人复核是否继续打
(单站页一键「停手」记入停打名单 / 「核实过·继续打」),左栏战果优先排序、按档位分级;
**有结论的站还能「归档结案」**(不是涉诈 / 已交付收工):退出工作视图,派发侧也不再生成新目标 ——
单站 / 整簇 / 待审一键批量三个入口,「已归档 N」chip 单独回看、可逐个撤销(真源 = `config.json` 的 `archive`);
判档口径升级会自动重读旧判定(判定盖 `REV` 版本章:重读 deep/mid/legit,surface/none 零成本放行)。
**单站页按统一目录树呈现**:报告/总报告.md(唯一完整报告)· 时间线 · 思维导图(源文件 + 「打开导图」)·
00_档案 · 目录清点(证据/交付/大件存证/哨位运行件/零散文件);簇级无点文件夹也进树。
**pi 扩展(`profiles/*.json` 的 `extensions` 字段)**:`shezha` 包用它挂 `tools/pi_extensions/egress_guard.ts`
——把"出口必须显式走代理"升级成**程序强制**:每条 bash 命令先过策略脚本(由 `EGRESS_GUARD_SCRIPT`
指向,hook 协议与涉诈仓库的 ZCode hook 同一套),无代理痕迹即拦截并把合规改法回给模型;fail-open。
**循环按任务驱动,不按时间**:众测/正式渗透只在四种情况下收工(全部完成 / 平台不可恢复 / 网络连续失败 / token 无效),
时间只用于预算与节奏;链式题(多 flag)钉住容器名额不被轮换 —— **但「钉死」有名额上限**(`budgets.chain.max_pinned_slots`,**默认 1**;设 0 = 链式题也完全按普通题排队):而且「钉住」不等于免检 —— 每个时间片都要结算,连着 4 片零新 flag 就摘掉钉名额资格、让位给广度(实测:链式题 1.5 flag/槽时 vs 广度题 3.8;某道链式题曾钉着 1 个名额 5.5 小时零产出)。rotate / 收尾增援 / 抢名额三处口径一致,超配额的那道链式题照常走时间片让位、即使它 worker 还在跑(实测:3 道链式题的 3 小时长会话曾把 3 个名额全锁死,广度题一道都排不上),单题预算按难度/链式分档(`profiles/zhongce.json` 的 `loop` 与 `budgets`)。
**平台没受理的提交由 harness 兜底补交**:agent 提交过、但平台判定为「没受理」的 flag 值会被记下,通道恢复后由 harness **原样补交一次**(`tools/backfill_flags.py`,审计里标 `by: harness`);提示词明确禁止 agent 自己写重试循环(第六次实测有 agent 每 20s 重试烧掉几小时);`WRONG` 永不补、永不重试。
**提交自带假阴性重试、直连带系统级代理免疫**;**适配器与 token 都按「环境变量 → `.env` → 显式失败」解析** ——
托管容器里 `.env` 若没被导出,旧版会**静默**回退到 CTF 适配器(旧域名)并把整轮提交作废(第六次实测 4h40m / 250+ 次全废),
现在"选错平台"这件事不可能发生,回归断言在 `tests/hosted_pack_test.py`。把上一轮的战果目录填进 `profiles/zhongce.json` 的 `kb_extra`,
下一轮就能直接检索到同题的结论(实测这是跨轮最大杠杆)。
**容器有服务级探活**:平台报 `available` 不等于容器里的服务活着(TCP 通但 HTTP 不回也算死)——
连续几次探不通就自动关掉换新容器,不用人工救;收尾期(只剩最后一两题、并发位空着)会自动给卡住的题加派补位 agent。
**催促 agent 的判据是产出,不是步数**:重复执行同一条命令、或长时间没有任何新证据时才提醒一次
(边沿触发 + 冷却 + 每题上限;容器不可用时一律不催),长利用链不会被"已跑 N 步"打断。
**知识库带 CVE 速查包**:`python3 tools/kb.py hit "comfyui"` / `hit "grafana 8.2"` 直接命中组件卡
(`knowledge/cves/`:经典中间件 + AI/ML 服务 + 令牌/沙箱,公开通告锚点 + 最短验证路径;
每张卡的 CVE 表按「实测 / 公告 / 候选待核」分档,候选条目不许直接打;先确认版本再套用)。
**凭据作业装备(厂商字典 + 产品词×年份生成器)**:跑分里最常见的卡点不是利用链,而是某节点"所有入口都要认证"。
镜像里预置 **6 张按厂商切的口令表**(泛微 / 国产OA / 网络设备 / 安防 / 运维中间件 / 通用弱口令,435 条)
+ 厂商词根表 + 用户名面;`python3 tools/gen_product_year.py --vendor 泛微` 按指纹**现场生成**"产品词×年份"族
(国产 OA/设备的 `weaver@2023`/`Ecology@2024` 这类设计,通用字典永远覆盖不到);
`tools/dict_prep.py` 按固定顺序组表 —— **厂商定向 → 本目标已观测复用值 → 年份族 → 通用高频,不接受重排**
(认证服务普遍硬限速,顺序就是策略)。完整打法(先判通道 → 先量限速窗口 → paced 投递,
含"并发把自己要打的通道打烂"这条实测事故)在 `knowledge/playbooks/credential-wall.md`。
口径:只收公开弱口令标准内容,**不含任何题号绑定的答案**。
**能直接打成平台托管的 Docker 镜像**:`bash docker/build.sh` → `dist/rotom-agent.tar.gz`(构建期把模型地址改写成
平台网关 `<域名>.tsecbench.gw`;镜像里带全套工具与离线资料,**无外网也能解题**;`docker/selfcheck.sh` 会断言
镜像内**没有历史答题记忆 / 答案样式 / 明文凭据**)。**镜像按跑分需要裁剪**:只带 `zhongce`/`ctf`/`pentest` 三包,
涉诈那条线(网页控制台 / 成果评读 / Excel 榜单切片 / `prompts|profiles|knowledge` 里的 shezha 件)**不进镜像** ——
本地 `./start.sh` + 8799 控制台照旧不受影响。说明见 [docs/DOCKER.md](docs/DOCKER.md)。
**控制台换皮为 Hermes 风格**(零阴影/零圆角/4px 网格/HarmonyOS Sans + Cascadia 等宽,深色浅色系中性配色);
**思维导图用 markmap 本体渲染**:`assets/markmap/` 内置 d3 + markmap-lib + markmap-view(零 CDN),
站点卡片直接用官方 API 把 `思维导图源文件.md` 画成原生导图(滚轮缩放/拖拽平移/点节点折叠,可切「适应窗口 / 100%」);
「新窗口打开」的 `报告/思维导图.html` 由 `/api/shezha/mindmap` 把 CDN 装载器换成本地库,离线也能出图。
**卡片长文不留省略号**:站点页「成果评读 / 目录清点 / 实名线索」用 `kv-wide`(一行一条 + 值换行),结论和文件清单完整可读 ——
默认 `.kv` 的多列单行省略号只留给短标签值。
**页面滚动零卡顿**:滚动位置按 200ms 节流记录(实测单事件 13ms → 0.04ms),左栏列表内容没变就不重画,后台标签页暂停轮询。
**涉诈簇关系图**按连通分量分组并分层排布，图上只画主干关系，所有关系可在「关系清单」逐条核对；簇图、站点档案、题目详情、筛选和滚动位置会写入地址栏/会话状态，刷新后回到原处。
状态(进度/并发/剩余)在顶栏,操作(开始/停止/重启 + 设置/记录/知识库)在侧栏底部;
设置抽屉分「常用(平台/模型/项目)/ 高级(运行参数)」两页,默认只看常用。

(零阴影/零圆角/4px 网格/HarmonyOS Sans + Cascadia 等宽,深色浅色系中性配色);
所有类名与逻辑一行没动,仅样式与静态资产(换皮前的副本留在本地,不入库)。
**黑板会自己保持干净**:写入统一 UTF-8(混进 GBK 自动转码并提醒 agent),容器换地址后黑板顶部给出
「当前目标地址 + 已失效地址」,旧地址自动作废、agent 不会再打死地址。
细节(字段、优先级、加新包、提示词模板、系统提示词、会话时长旋钮、黑板卫生)见 **[docs/PROFILES.md](docs/PROFILES.md)**。

## 与原 Rotom(CTF 版)的差异

| | 原 Rotom(CTF 版) | 当前主分支(Rotom_beta) |
|---|---|---|
| 范围 | 只做 CTF 比赛 | CTF + 众测/TSec + 正式渗透 + 涉诈侦查(四策略包) |
| 平台接口 | 查题/重置/提交三个 GET 接口 | TSec 五个接口,HTTP 头 `BENCHMARK_TOKEN` 鉴权(可切回原 CTF 适配器) |
| 靶场容器 | 平台常开,题目自带入口 | TSec 模式下**要自己 start/close,平台同时只允许 3 道题的容器开着** |
| 一题多 flag | 一个 flag 命中即通关 | TSec 模式下 `flag_count` 可能 >1,**全部拿到才算通关** |
| 网络前置 | 直连即可 | TSec 模式下**题目入口在靶场 VPN 内**,开工前必须过 VPN 预检 |
| 并发 | 现场 36 并发抢跑 | 不在赛场了:**起始 4 并发**(3 个容器 × 1~2 个 agent) |

## 快速开始

```bash
cp .env.example .env        # 已生成一份 .env,只需填下面两项
./start.sh                  # VPN 预检 → 平台连通 → 起控制台 → 后台开跑
```

网页控制台 <http://127.0.0.1:8799> 里能改全部配置(平台地址/token、模型、并发、时间片、策略包),
并有 **测试 VPN** 与 **测试平台接口** 两个按钮,改完不用碰命令行。

**两项必填**(平台创建跑分任务时下发):`BENCHMARK_BASE_URL`、`BENCHMARK_TOKEN`。
模型默认用 pi 里已有的 provider `ds`(`deepseek-flash`);想换模型在控制台填 Base URL / Key / 模型名即可。

## VPN(唯一需要你自己搞定的前置)

题目入口地址**只在靶场 VPN 内可达**,harness 把 VPN 预检当成硬前置:不过就不查题、不派 agent、不启动容器,
连上后自动开跑。`.env` 里 `VPN_CHECK_URL` 默认是 `http://10.0.100.58`(纯 HTTP),
`tools/vpn.sh up/down/status` 用 `.env` 里的 `VPN_OVPN_FILE` 连;**本机没装 openvpn** 用
`brew install openvpn`,或直接 Tunnelblick / OpenVPN Connect 打开平台下发的 `.ovpn`。
连上后跑 `python3 tools/netcheck.py` 全量体检:VPN / 平台 / 模型端点 / 代理 / 靶场目标。
**坑**:VPN 是全隧道时,模型 API 的流量也会被带进 VPN;体检发现模型端点不通,先确认
`NET_PROXY=direct`(默认),再调 VPN 分流。

## 它怎么跑

```
每轮循环:VPN 预检 → 轮换(rotate)→ 容器调度(manage_containers)→ 派 agent(spawn)→ 启动 pi → 收尾(reap)
```

- **容器名额 3**:`manage_containers` 只让"最该跑的 3 道题"的容器开着(没碰过 > 关卡低 > 有进展),
  其余一律 close 释放名额。
- **时间片轮换**:占名额 `QUESTION_SLICE_SECONDS`(默认 30 分钟)且**没有新 flag 进展**时,
  自动让位给还没碰过的题;有进展就续期。
- **一题多 flag**:提交成功后脚本打印 `本题进度 x/y`;**只要没通关,agent 必须继续找下一个漏洞**。
- **hint 折中策略**(2026-10-26):首发不代取、第 2 次尝试(完整尝试失败一次)自动取 —— 一枪过不扣分、真卡住才付 10%;agent 也可随时 `python3 tools/get_hint.py <题号>` 自取。`HINT_AFTER_ATTEMPTS=0` 回到纯自主,`HINT_EARLY_DIFFS=hard,medium` 可恢复首发全取。
- **短会话接力 + 后台作业 + 防打转 + 网关监测**(2026-10-26,对齐头部选手):单题切成 `SESSION_SLICE_SECONDS`(默认 1200s)一节,到片交接**不消耗失败次数**;长扫描走 `tools/job.py start/status/list/stop`(题目级隔离,不许阻塞);提示词含"两次无新证据换面/禁止原样重跑";开跑与跑中自动量模型延迟,超基线 2× 且 ≥5s 即告警。
- **故障必响**(2026-10-25):平台接口连续失败每 10 分钟必报、开容器失败必打卡、调度停滞(久无新派工/新解出且并发≤1)周期告警;补交与查题解耦独立重试;`跑一轮.sh` 自动拉起主机侧看护 `tools/round_watch.sh`(异常弹通知,不依赖任何应用开着)。
- **会话时长保护**(`ADOPT_WORKERS`/`ROTATE_WAIT_RUNNING`/`WORKER_MIN_LIFETIME`,默认全开):
  runner 重启时**接管**在跑 agent、**有 agent 在跑的题不被轮换**、新 agent 有保护期 —— 解决"38% 死于重启"的问题。
- **停止 = 释放**:`./start.sh stop` 收掉所有 agent 并 `close` 所有靶场容器。

## 关键配置(.env,等价操作都在网页控制台)

| 参数 | 默认 | 含义 |
|---|---|---|
| `PROFILE` | `zhongce` | 策略包(网页「创建项目」时也能选) |
| `CONTEST_ADAPTER` | `tsec` | 平台适配器:`tsec` / `arena` / `newrank` / 留空=原 CTF 平台 |
| `NEWRANK_BASE_URL` / `NEWRANK_STAGE` | 空 / 1 | 新榜单地址与阶段号(`getquestion` 的 stage) |
| `NEWRANK_REFRESH` | 0 | 0=复用落盘的题单+Cookie;1=强制重新拉题(会换一批票据) |
| `ROTOM_AI_TRACE` | 1 | 1=给 worker 挂 `ai_trace` 扩展记录每次调用的计时;0=关 |
| `MAX_ACTIVE_CHALLENGES` | 3 | 同时开着的容器数(**平台硬上限 3**) |
| `QUESTION_SLICE_SECONDS` | 1800 | 单题时间片;0=不轮换 |
| `START_WORKERS` / `MIN_WORKERS` | 4 / 1 | 起始并发与下限 |
| `WORKER_TIMEOUT` / `MAX_WORKER_TIMEOUT` | 900 / 1800 | 单题完整尝试预算,失败后递增 |
| `SESSION_SLICE_SECONDS` | 1200 | 单个 pi 会话上限;到片接力(保留工作区/黑板,不计失败);0=不切片 |
| `ROUND_WINDOW_MINUTES` | 0 | 总时长(分钟),0=不限 |
| `HINT_AFTER_ATTEMPTS` | 2 | harness 代取:两个完整尝试都失败后才取(0=纯 agent 自主;1=第一次失败即取) |
| `HINT_EARLY_DIFFS` | (空) | 自动注入模式下首发即取的难度(默认关) |
| `HINT_SKIP_DIFFS` | easy | 自动注入模式下永不代取的难度 |
| `API_FAIL_RELOG_S` | 600 | 平台接口连续失败时至少每这么久再报一次(秒) |
| `STALL_WARN_S` | 900 | 调度停滞告警阈值(秒;0=关) |
| `STALL_MIN_PENDING` | 10 | 未解题少于这个数不按停滞论 |
| `ADOPT_WORKERS` / `ROTATE_WAIT_RUNNING` / `WORKER_MIN_LIFETIME` | 1 / 1 / 120 | 会话时长保护三件套 |
| `NET_PROXY` | direct | 直连(忽略本机代理)。VPN 是隧道不是代理,保持 direct |
| `PI_BASE_URL` / `PI_MODEL` / `PI_API_KEY` | 本地 DeepSeek 网关 | 模型端点的运行时覆盖:`跑一轮.sh` 原样传给容器,启动时由 `platform_env.py` 写进 pi 的 `models.json`;**换网关不用重建镜像** |
| `REDUCE_COOLDOWN` / `RECOVER_AFTER` | 60 / 1800 | 限流时降并发的冷却 / 限流平息多久后并发 +1(0=只降不升) |
| `CRED_HARVEST` | 手动 | 跨轮口令积累:`tools/cred_harvest.py`(过滤 flag/uuid/题号)与 `tools/gen_weak_variants.py`(变形族) |

## 看进度 / 收工

```bash
tail -f logs/runner.out                # 调度日志(容器就绪、轮换、提交、重试)
tail -f logs/submissions.log           # 每次提交与平台返回
./start.sh status                      # 一屏状态
./start.sh stop                        # 停止(收 agent + 关容器)
python3 tools/status_now.py            # 更细的实时状态
python3 tools/ai_monitor.py --once      # AI 用量/计时(控制台顶栏「AI」同源;--watch 常驻)
python3 tools/postmortem.py            # 跑完后复盘
python3 tools/export_trace.py          # 审计轨迹(Thought/Action/Observation)
```

## 离线自测(不连 VPN、不花模型 token)

```bash
python3 tests/profile_test.py          # 断言:策略包字段 + 黄金对照 + pentest 链路
python3 tests/test_ai_monitor.py       # AI 用量/计时聚合(纯离线)
python3 tests/newrank_test.py          # 新榜单适配器:票据换 Cookie / 缓存 / 提交形状
python3 tests/session_test.py          # 会话保护旋钮模拟
python3 tests/tsec_test.py             # 35 项断言:适配器/容器调度/多 flag/提交工具/轮换
bash tests/run_mock_e2e.sh             # 端到端:mock 平台 + 假 pi,跑完 6 道题 9 个 flag
bash tests/run_startsh_mock.sh         # ./start.sh 入口的 mock 验收
```

## 文档地图

| 文档 | 内容 |
|---|---|
| [README.md](README.md) | 本文件:门面 |
| [README.en.md](README.en.md) | English version |
| [docs/PROFILES.md](docs/PROFILES.md) | 策略包详细说明(字段、切换、加新包、提示词模板、系统提示词、会话时长旋钮、回退点) |
| [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md) | 全项目结构地图(每个文件是什么、怎么配合) |

## 与原 Rotom 的关系 / 许可

解题内核(黑板、并发、重试、知识库四层投喂、Observer、轨迹导出)沿用自原 Rotom(机制层);
当前主分支增加了 `profile.py` + `prompts/` 的策略层(五策略包)、`tsec_api.py` / `newrank_api.py` 适配器、`tools/ai_monitor.py` 调用监控、
`tools/report_finding.py` 正式渗透工具链,以及会话时长保护三件套。
欢迎 Fork 与魔改;**商用请通过项目主页联系作者**。