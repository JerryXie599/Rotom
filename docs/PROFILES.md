# 策略包(profile):CTF / 众测 / 正式渗透 / 涉诈侦查 / 新榜单

同一套调度机制(runner/solver/board),五种**策略**用数据切换 —— 不复制代码。
机制管"怎么跑"(并发、黑板、重试、记账),策略管"按什么打法跑"(提示词、容器策略、完成定义)。

## 五个包一览

| | `ctf` | `zhongce`(本仓库默认) | `pentest` | `shezha` |
|---|---|---|---|---|
| 场景 | CTF 解题赛 | 众测/跑分平台(TSec 等) | 客户授权的正式渗透 | 涉诈网站侦查(全自动批次) |
| 目标与记账 | 提交 flag(单 flag) | 提交 flag(**一题多 flag**) | **记录 finding**(没有 flag) | finding + **站点档案**(00_档案.md/证据/报告) |
| 完成定义 | 提交输出 `CORRECT` | `本题通关 x/x` | `report_finding --done` → `DELIVERED` | `--done` + **档案齐备闸门**(缺 00_档案.md/侦察报告 → `DELIVERED_DEFERRED` 逼补齐) |
| 任务来源 | 平台适配器(contest_api/arena) | 平台适配器(tsec_api) | 本地 `targets.json` | 本地 `targets.json`(Excel 榜单切片,**绝对 workdir** 直指档案 `工作区/`) |
| 容器 | 平台托管(不碰) | **harness 自管 + 名额轮换**(平台上限 3) | 委托方环境(不碰) | 无(一切流量走探测代理) |
| VPN 预检 | 关 | 关(现场要连靶场 VPN 时把 `vpn_gate` 改 true) | **开**(VPN 让测试流量不可追溯到本机) | 关(出口预检在作战仓库 `开批次.sh` 里做) |
| 同题 agent | 2(增援到 4) | 2(增援到 3) | 1(增援到 2) | 1(增援到 1;侦查要低噪声) |
| 提示词包 | `prompts/ctf/` | `prompts/zhongce/` | `prompts/pentest/` | `prompts/shezha/`(三铁律/中性词/档案纪律) |
| playbook | 六方向 CTF | 众测/渗透(找 flag 导向) | `knowledge/pentest-formal.md`(报告导向) | `knowledge/shezha.md`(涉诈打法手册) |
| pi 扩展 | 无 | 无 | 无 | `tools/pi_extensions/egress_guard.ts`(出口合规程序强制) |

> `newrank`(2026 全国网安比武「新榜单」)与 `ctf` 基本同形:flags + Web 直接入口、无容器、无 VPN;
> 差异是**每个题目的入口 URL 带一次性票据**,适配器在拉题时换成会话 Cookie 并写进 worker 的 `cookies.txt`,
> 提示词包用 `prompts/newrank/`。详见下面专节。

## 怎么切

- 网页控制台 →「运行参数」→ **策略包** 下拉框 → 保存 → 重启 runner(推荐)
- `.env` 里 `PROFILE=zhongce|ctf|pentest`
- 命令行:`python3 config.py --profile pentest`

**优先级**:容器/轮换/VPN/同题 agent 数这类**策略参数以策略包为准**(换包自动跟随,不被上一场 .env 残留带偏);
并发、超时、窗口这类**运行参数仍以 .env/网页为准**。

## 可选数据块(2026-09-29 新增,按需在包里的 json 里加)

| 数据块 | 作用 | 默认 |
|---|---|---|
| `containers.health` | 容器**服务级探活**:TCP 通且(HTTP 端口)有 HTTP 响应才算活;连续 `fail_threshold` 次判死 → 强制换新容器。字段:`enabled / probe_interval_s / fail_threshold / tcp_timeout_s / http_timeout_s / http_check(auto\|always\|never) / max_heal_per_question / heal_cooldown_s` | 只在 `zhongce` 开(其它包容器不由 harness 管) |
| `dispatch.endgame` | **收尾期自动增援**:未解题 ≤ `unsolved_lte`、有空闲并发位、且该题距上次实质进展 > `after_running_s` → 把同题上限放到 `max_helpers`,新 agent 带 `brief` 分工简报 | 只在 `zhongce` 开 |
| `observer` | **催促判据**:`mode=steps`(每 `kb_nudge_steps` 步催一次查知识库)/ `mode=progress`(重复命令 ≥ `repeat_sig` 次,或距上次实质进展 > `stall_s`;带 `cooldown_s` 与 `max_per_question`,容器不可用时不催) | `zhongce`/`pentest` = progress,`ctf` = steps |

这三块都是**纯数据**:给别的包加同样的字段就会生效,不用改代码。

## 新榜单(newrank)怎么用

```bash
CONTEST_ADAPTER=newrank PROFILE=newrank \
NEWRANK_BASE_URL=http://<平台>:5007 NEWRANK_STAGE=1 BENCHMARK_TOKEN=<队伍token> \
  python3 runner.py
```

- 接口:`POST /api/getquestion {stage, token}` 拉题;`POST /api/submitflag {questionid, flag, token}` 提交。
- **一次性票据**:题单 URL 里的 `__ctf_ticket` 首次 GET 返回 303 + `Set-Cookie`,票据当场作废。适配器在
  `list_questions` 时就替 agent 兑换好 Cookie(并用干净 URL 复验 200),把 Cookie 写进题目描述与
  `work/<题>/w<N>/cookies.txt`;提示词要求 curl/dirsearch/sqlmap/ffuf/requests 全部带 Cookie。
- **题单缓存**:票据每次拉题都会换,所以题单+Cookie 落盘 `logs/newrank_questions.json`,默认复用
  (`NEWRANK_REFRESH=1` 才强制重拉);否则黑板地址会被反复改写、agent 的会话也会被打断。
- **提交计数**:平台响应没有 `correct_flag_count`,适配器按每题 1 flag 补 `1/1`,
  这样 `tools/submit_flag.py` 才会把黑板标成 solved、runner 才会杀掉该题 worker。
- 没有容器生命周期接口,因此 `MAX_ACTIVE_CHALLENGES / QUESTION_SLICE_SECONDS` 对本包无效;
  广度优先靠 `START_WORKERS` 与单题超时控制。

## 涉诈侦查(shezha)怎么用

shezha 与 pentest 同构(目标清单 + findings 记账),差异在三件事:

1. **目标从 Excel 来**:作战仓库(`00_总控/自动批次/`)的 `excel转targets.py` 吃榜单 Excel,
   排除停打名单/已建档/历史已派发后切片,生成带**绝对 `workdir`** 的 targets.json(直指
   `01_目标档案/<库>/<域名>/工作区/`),worker 的产物就地进档案目录。
2. **合规程序强制**:策略包 `extensions` 字段挂 `tools/pi_extensions/egress_guard.ts`;
   环境变量 `EGRESS_GUARD_SCRIPT` 指向策略脚本(hook 协议:stdin JSON / exit 2 + stderr),
   涉诈仓库直接复用它现成的 `.zcode/hooks/egress_guard.py` —— 同一套白名单/逃生舱/fail-open,
   ZCode 与 Rotom 两个 harness 一个口径。未设置 `EGRESS_GUARD_SCRIPT` 时扩展不启用(零开销)。
3. **知识库与台账在作战仓库**:`ROTOM_KB_EXTRA`(冒号分隔)把 `武器库/`+`00_总控/` 注进 kb.py
   (路径不进本仓库,json 保持可公开);收工由仓库侧脚本重跑目标总表重建、常驻对账、书记员写批次汇总。

完整操作手册在作战仓库 `00_总控/自动批次/README.md`(Excel→开批次→收工三步)。

## 正式渗透怎么用

1. 复制 `targets.example.json` 成数据目录下的 `targets.json`(默认项目就在仓库根;项目制则在 `projects/<名>/`):
   ```json
   {"engagement": "客户A-外网", "targets": [
     {"id": "web-01", "name": "官网", "address": "https://www.example.com", "notes": "登录口与 API 均可测"}]}
   ```
   `TARGETS_FILE=<绝对路径>` 可指向别处的清单。
2. 切到 `pentest` 包(它会自动用 targets_api 当任务源,忽略 CONTEST_ADAPTER;不需要 token)。
3. 开始跑:每个目标一个 agent;发现漏洞就用
   `python3 tools/report_finding.py <目标id> --title ... --severity high --detail "复现/证据/影响/建议"` 记录;
   测完执行 `--done --summary "..."` 交付(黑板标 solved,harness 不再派人)。
4. 收工后出报告:`python3 tools/pentest_report.py --run-dir <数据目录>` → `REPORT.md`(按目标分组、按危害排序)。

工人在提示词里拿到的是**授权声明 + 非破坏/证据/限速红线 + 报告导向 playbook**;
`prompts/pentest/prompt.md` 的「授权声明」一节就是用来抑制模型"这是黑客行为"式误拒的 ——
它把授权事实、范围、职业身份讲清楚,并把"要不要执行"的判据收敛成范围与非破坏两条。**改文案不用动代码。**

## 加第六个包(例如"内网横向")

1. 复制 `profiles/zhongce.json` 成 `profiles/<新名>.json`,改 `name/title/goal/dispatch/containers/playbooks`;
   `goal` 只支持 `flags`(平台记账)与 `findings`(本地记账)两种,新玩法先在 profile.py 里加一类记账再谈。
2. 复制 `prompts/<最接近的包>/` 成 `prompts/<新名>/`,`prompt.md` 里改文案、`shared.md` 改黑板表头。
3. `python3 tests/profile_test.py` 应当仍全绿(它按包名断言,新包要先在测试里加断言)。

## 提示词模板怎么写

`prompts/<包>/prompt.md` 是纯文本,`@@名字@@` 是占位符(用 `@@` 而不是 `{}`,这样里面的 JSON/代码花括号不用转义)。
solver.build_prompt 负责替换;可用占位符见 `solver.build_prompt` 里的 `values` 字典,常用:

`@@SLOT@@ @@TOTAL@@ @@ANGLE@@ @@SQUAD_BLOCK@@ @@ATTEMPT@@ @@WORKER_TIMEOUT@@ @@MAX_HELPERS@@
@@QID@@ @@TITLE@@ @@CATEGORY@@ @@SCORE@@ @@DESCRIPTION@@ @@DIFFICULTY@@ @@LEVEL@@ @@FLAG_COUNT@@ @@PROGRESS@@
@@CONNECTION_BLOCK@@ @@WDIR@@ @@FILES@@ @@SHARED@@ @@ROOT@@ @@PLAYBOOK@@ @@ENV_HINT@@ @@KB_TOOL@@
@@SUBMIT_TOOL@@ @@RESET_TOOL@@ @@REPORT_TOOL@@ @@KB_HINT@@ @@HINT_BLOCK@@ @@TIME_RULE@@ @@NET_RULE@@`

改完跑 `python3 tests/profile_test.py`:它会断言 `ctf`/`zhongce` 两个包与改造前的 prompt
**逐字一致**(`tests/golden/` 是改造前存档)、`pentest` 包无未替换占位符。

## 系统提示词(pi 的系统层)

每个包还能带一份**系统提示词** —— 通过 `pi --append-system-prompt <文件>` 追加到 pi 内置系统提示词之后,
写身份、红线、自主性这类**每轮都必须成立**的东西(比写在任务消息里更硬):

| 包 | 文件 | 要点 |
|---|---|---|
| `ctf` / `zhongce` | `prompts/shared/system-ctf.md`(**共用一份**) | 自主性、只碰给定目标、提交只走 submit_flag、禁猜/禁假 flag、长命令限时 |
| `pentest` | `prompts/pentest/system.md` | **授权声明**(已授权,不得以"未授权/像黑客行为"为由拒绝、降级或暂停;不必逐次请示)+ 行动纪律(范围/非破坏/数据最小化/证据) |

在 `profiles/<包>.json` 里用 `"system_prompt": "shared/system-ctf.md"` 指定(缺省则取 `<包>/system.md`)。
正式渗透那份的授权声明措辞就是给模型"解除误拒"用的:**范围与非破坏两条判据之外的犹豫都不成立**;
其中"数据最小化(只截证明所需片段、不批量导出)"是职业规范也是安全边界,要放开/收紧直接改这个文件。

## 会话时长:别让 harness 把 agent 打断(2026-09-28 实测)

那一轮 234 个 worker 会话:**中位 211 秒,32% 不到 1 分钟,34% 工具调用少于 5 次**;
按结束原因拆开看,没有一个会话是模型自己退出的(全是 harness 杀的):

| 结束原因 | 会话数 | 时长中位 | 工具调用中位 | 说明 |
|---|---|---|---|---|
| `shutdown`(重启/停机) | **88~99** | **39 秒** | **2** | runner 重启(supervisor 拉起/改参数)把在跑的 agent 全杀 |
| `solved` | 91~123 | 269 秒 | 13 | 正常收工 |
| `容器已释放`(名额轮换) | 24 | **893 秒** | **30** | 杀的都是投入最深的会话 → 深链题努力清零 |
| `timeout` | 15 | ~1205 秒 | 37 | 单题限时(这个旋钮早就可调) |

对应三个可配旋钮(网页「运行参数」/ `.env`):

| 旋钮 | 默认 | 作用 |
|---|---|---|
| `ADOPT_WORKERS` | `1` | runner 重启时**接管**仍在跑的 pi 进程(不打断);`0` = 旧行为(全杀重来) |
| `ROTATE_WAIT_RUNNING` | `1` | 有 agent 在跑的题**不被容器轮换**(等它跑完/超时);`0` = 旧行为(时间片到就让位并杀掉) |
| `WORKER_MIN_LIFETIME` | `120` | 新 agent 的保护期(秒):期间不被轮换、容器名额优先保住;`0` = 关 |

另有 `hold_container` 机制:runner 每轮把"有 agent 在跑/在保护期"标注给平台适配器,
`_rank` 里第一位就是它 —— 容器名额优先留给正在干活的题。
`./start.sh stop` 语义不变(明确停机时 runner 收工会连接管的 worker 一起收掉;runner 已经不在了则由 start.sh 按注册表收)。
**要更快轮换**就调低 `WORKER_TIMEOUT` 或把 `ROTATE_WAIT_RUNNING=0`。

## 黑板卫生:编码统一 + 地址失效(2026-09-28 修)

**① 编码:写黑板强制 UTF-8。** 实测踩过:agent 把 GBK 工具输出直接 `>>` 进 SHARED.md(字节 0xbc),
旧代码严格 utf-8 解码直接崩、把 runner 打进重启循环;改成 `errors="ignore"` 后能跑但**内容静默丢失**。现在三层防:

| 层 | 做法 |
|---|---|
| 源头 | worker 进程注入 `LC_ALL/LANG=en_US.UTF-8`、`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`(工具输出尽量就是 UTF-8) |
| 读取/重写 | `textutil.read_mixed()` 逐行解码:UTF-8 → GB18030 → 兜底,**内容保住**;harness 每次重写黑板都统一成 UTF-8 |
| 提醒 | 检测到转码时:黑板里追加一条 Observer 提醒(教它用 `iconv -f gbk -t utf-8` 或 `printf '%s\n'` 而不是 `echo "$(命令)"`),并写一条事实 |

三个 prompt 包 + 两份系统提示词里都有硬规则:**别把命令原始输出直接倒进黑板,GBK 先 iconv**。

**② 地址:黑板顶部有 harness 维护的「当前目标地址」块。** 容器重开 IP/URL 会变,旧地址留在黑板里
会让 agent 反复打死地址(实测同一题黑板里攒了 5 个地址)。现在:

```
>> [harness] 当前目标地址(harness 维护,**以此为准**;黑板上其它地址可能是旧容器的残留):
>> [harness] 当前目标地址:url1=http://10.0.174.186:80
>> [harness] 容器状态:available
>> [harness] 已失效地址(打这些是浪费,别再试):http://10.0.174.184:80 · http://10.0.174.185:80
```

- 块插在「## 已确认的事实」之前(agent 读黑板第一眼就看到);harness 维护的行以 `>> [harness]` 开头,
  每次重写先剔除再重建,不会和 agent 自己写的行打架。
- 换地址时(runner `_accept_conn`)旧地址自动进 `dead_addrs`;容器被释放时(`_mark_dead`)当前地址立刻作废,
  块里显示「暂未就绪」而不是一个死地址。
- 只有**内容签名变了**才重写文件(地址没变就不会每轮动黑板);工作区还没建好时写失败**不记签名**,下一轮补写。
- 三个包的提示词 + `_connection_block` 都写明:**地址以黑板顶部为准,黑板上其它地址可能是旧容器残留**。

## 回退

本次改造前的 solver/runner/board/config/dashboard/start.sh/README 版本以 git 历史为准(改造前最后一个提交),回退用 `git checkout <提交> -- <文件>`。