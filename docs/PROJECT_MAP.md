# Rotom_beta 项目地图(Project Map)

> 这份文档是 Rotom_beta 全项目的**结构地图**:每个文件是什么、谁调用谁、关键数据流向、
> 状态机、扩展点。读这份文档就能**知道改哪里**。
>
> 不熟悉的概念先看 [../README.md](../README.md);策略包细节看 [./PROFILES.md](./PROFILES.md);版本演进看 git 提交历史。

---

## 0. 一句话架构

```
策略包 (profiles/*.json + prompts/*/)  决定"按什么打法跑"
       ↓
机制层 (runner + solver + board)       决定"怎么跑"
       ↓
宿主适配层 (tsec_api / newrank_api / contest_api / arena_api / targets_api)  决定"和谁对话"
       ↓
配置 (config.py + .env)                决定"用谁的模型、跑在什么网络"
       ↓
网页控制台 (dashboard.py + start.sh)   决定"用户怎么改、怎么看"
```

**机制层三件套是核心,所有改动都围绕它们**;策略层是数据,改文案不动代码;
宿主层是适配器,加新平台只加一个文件;配置层是入口,所有 .env 旋钮都同时支持控制台改。

---

## 1. 一级目录结构

```
Rotom_beta/
├── 机制 + 入口(顶层 .py / .sh / .json)
│   ├── start.sh                  一键启动(VPN 预检 → 平台连通 → 控制台 → 后台跑);含 status/stop/vpn
│   ├── runner.py                 调度主循环 + rotate / manage_containers / vpn_gate
│   ├── solver.py                 提示词构建(build_prompt) + worker 进程入口
│   ├── textutil.py               编码工具:黑板可能混进 GBK,逐行转码(UTF-8→GB18030→兜底),绝不丢内容
│   ├── board.py                  黑板(题目状态机、并发锁、findings 记账)
│   ├── config.py                 .env 读写 + pi provider 同步 + VPN 预检
│   ├── profile.py                策略包加载器(load / current / names)
│   ├── dashboard.py              网页控制台(状态/启停/参数/项目/提交历史/审计;涉诈簇图/关系清单/刷新定位)
│   ├── tsec_api.py               TSec Benchmark 适配器(众测/跑分)
│   ├── newrank_api.py            2026 比武「新榜单」适配器(getquestion/submitflag;一次性票据→Cookie)
│   ├── contest_api.py            原 CTF 平台适配器(留作兼容)
│   ├── arena_api.py              NSSCTF Agent Arena 适配器(压测/训练)
│   ├── targets_api.py            正式渗透任务源(本地 targets.json)
│   ├── board.json / board.lock   运行时数据:黑板内容 + flock 锁
│   ├── .env / .env.example       运行时配置
│   ├── .gitignore                忽略 logs/、work/、board.json、.env、__pycache__、knowledge/.kb_index.json、knowledge/vendor/
│   ├── .dockerignore              托管镜像排除清单(运行数据/凭据/测试 + **涉诈线**: dashboard.py、tools/review_results.py、tools/excel_targets.py、prompts|profiles|knowledge 里的 shezha 件、CHANGELOG.md)
│   ├── *.command                 macOS 双击启动/停止控制台的快捷方式
│   └── assets/fonts/             Hermes 字体(HarmonyOS Sans SC woff2,本地没有就用 PingFang 兜底)
│
├── profiles/                     策略包 = 纯数据
│   ├── ctf.json                  CTF 比赛:flag + 平台托管容器
│   ├── zhongce.json              众测/跑分(默认):flag + harness 自管容器 + 名额轮换
│   ├── pentest.json              正式渗透:finding + 本地 targets.json
│   ├── shezha.json               涉诈侦查:finding+站点档案 + extensions(pi 扩展)
│   └── newrank.json              2026 比武新榜单:flag + Web 直接入口(无容器/无 VPN)
│
├── prompts/                      提示词包
│   ├── ctf/{prompt,shared}.md                       CTF 比赛
│   ├── zhongce/{prompt,shared}.md                   众测/跑分(默认)
│   ├── pentest/{prompt,shared,system}.md            正式渗透(含授权声明)
│   ├── shezha/{prompt,shared,system}.md             涉诈侦查(三铁律/中性词/档案纪律)
│   └── newrank/{prompt,shared}.md                   2026 比武新榜单(会话 Cookie 纪律)
│   └── shared/system-ctf.md                         ctf + zhongce 共用系统提示词
│
├── docker/                       托管运行打包(Dockerfile / entrypoint / build / selfcheck)
│   └── Dockerfile                平台托管镜像:工具链 + 离线资料 + 构建期审计
├── knowledge/                    离线知识库
│   ├── README.md                 知识库说明
│   ├── pentest.md                众测/漏洞挖掘 playbook(主用)
│   ├── pentest-formal.md          正式渗透 playbook(报告导向)
│   └── shezha.md                  涉诈侦查 playbook(检索顺序/突破族速查/出口判读)
│   ├── ops-bench.md               跑分平台运维 playbook(封装/槽位/故障处置/证据纪律,跨平台通用)
│   ├── cards/                     技术卡(指纹→最短路径):kb.py hit "<指纹>" 命中后精读
│   │   ├── INDEX.md               卡索引(何时看哪张)
│   │   └── cve-fingerprint / jdwp-rce / sandbox-escape / cloud-storage-identity /
│   │       lateral-chain / session-and-captcha / bypass-filters / reverse-analytic
│   ├── cves/                      CVE 速查包(公开通告 → 最短验证路径;kb.py hit 也搜这里)
│   │   ├── INDEX.md               组件/指纹 → 卡 的路由表 + 三条纪律(先读版本/只打未修的/用上游测试当清单)
│   │   ├── find-cve.md            通用:拿到指纹/版本后怎么最快定位到能打的漏洞
│   │   ├── web-midware.md         经典中间件/运维组件(反序列化·穿越·未授权·图数据库·企业应用)
│   │   ├── ai-service.md          AI/ML 服务(ComfyUI / Ollama / Gradio / Langflow / Dify / MLflow / Ray…)
│   │   └── token-and-sandbox.md   JWT/令牌攻击族 + 代码沙箱逃逸(vm2 等)+ 反序列化闸门
│   ├── web.md / pwn.md / reverse.md / crypto.md / forensics.md / misc.md   六方向 CTF
│   ├── playbooks/
│   │   └── credential-wall.md     凭据墙打法(判通道→量限速→用户名面差分→排序组表→paced 投递;含反模式清单)
│   ├── wordlists/                 字典库(离线;口径:只收公开弱口令标准内容,不含题号绑定答案)
│   │   ├── README.md              目录说明 + 口径红线 + 组表/排序用法
│   │   ├── usernames.txt          用户名面(57 条,按场景分组:高概率段/服务账户/文件共享/云与自动化)
│   │   ├── vendor-vocab.txt       厂商 → 产品词根表(8 段:国产OA/面板/数据库/网络设备/安防/云)
│   │   └── vendor-passwords/      按厂商切的口令表(表头 `# aliases:` 自报别名;表内顺序即投递顺序)
│   │       ├── 泛微OA_weaver.txt / 国产OA与管理系统.txt
│   │       ├── 网络与安全设备_netsec.txt / 安防与物联网_iot.txt
│   │       ├── 运维与中间件_ops.txt / 系统与通用弱口令_base.txt
│   ├── fetch_vendor.sh           拉 PayloadsAllTheThings / ctf-wiki / RsaCtfTool
│   ├── vendor/                   第三方资料(被 .gitignore 忽略,clone 后跑 fetch_vendor.sh 重建)
│   └── .kb_index.json            离线索引(运行 kb.py 时生成)
│
├── docs/                         长文档
│   ├── PROFILES.md               策略包详细说明
│   ├── PROJECT_MAP.md            本文件
│   └── logo.png                  README 头图
│
├── tools/                        一组可独立运行的 CLI 工具(给运维/人手用)
│   ├── kb.py                     知识库检索(search / show / grep / list / reindex)
│   ├── get_hint.py               agent 自取平台提示(2026-10-26:默认不代取,取过一次该题扣 10%)
│   ├── job.py                    题目级后台作业 start/status/list/stop(长扫描不阻塞;越界拒绝)
│   ├── cred_harvest.py           跨轮口令入库(过滤 flag/uuid/题号;只收可复用弱口令)
│   ├── gen_weak_variants.py      生成 weak-leet / weak-affix 变形族
│   ├── gen_product_year.py       产品词×年份 候选口令生成器(按指纹现场产;输出按命中概率排序,不许重排)
│   ├── dict_prep.py              凭据组表器(厂商定向→已观测复用值→年份族→通用高频;固定顺序 + flag 行自检)
│   ├── backfill_flags.py         harness 侧「已确证 flag 补交」(只补 agent 提交过、平台没受理的值;WRONG 永不补)
│   ├── submit_flag.py            提交 flag(平台记账;zhongce/ctf/newrank 模式)
│   ├── report_finding.py         记录 finding + --done 交付(pentest/shezha;含档案齐备闸门)
│   ├── pi_extensions/
│   │   ├── egress_guard.ts       出口合规闸门 pi 扩展(tool_call 拦截→策略脚本)
│   │   └── ai_trace.ts           AI 调用追踪扩展(每次 provider 调用 → <worker日志>.calls)
│   ├── ai_monitor.py             AI 用量/计时监控(增量读 logs/*.jsonl + *.calls → logs/ai_usage.json)
│   ├── pentest_report.py         汇总 findings → REPORT.md(pentest 收工)
│   ├── netcheck.py               全量体检(VPN/平台/模型/代理/目标)
│   ├── vpn.sh / vpn_debug.sh     VPN up/down/status + 排障
│   ├── reset_env.py              CTF 平台重置(仅 contest_api)
│   ├── status_now.py             一屏状态
│   ├── postmortem.py             跑后复盘
│   ├── round_watch.py            轮次级看护
│   ├── round_watch.sh            主机侧轮次看护(容器消失/心跳停/接口连败/调度停滞 → 弹通知;跑一轮.sh 自动拉起)
│   ├── 跑一轮.sh                  本地整轮容器模式一键起跑(canonical 副本,复制到轮次目录用)
│   ├── monitor.py                多项目状态聚合
│   ├── watchdog.py               自愈看护(崩了拉起、磁盘满告警)
│   ├── export_trace.py           导出 Thought/Action/Observation 轨迹
│   ├── package_submission.py     打包提交包(给客户/平台审计)
│   ├── collect_sink.py           收口工具,统一收不同来源的输出
│   ├── decoders/                 VBE 等专用解码器
│   ├── excel_targets.py          涉诈:Excel 榜单 → targets.json(智能认列/stop_set/已建档排除;
│   │                             硬排除 = stop_set ∪ config.json 的 archive 归档名单,--redo 也翻不了)
│   ├── review_results.py         涉诈:成果评读器(AI 通读报告判档 deep/mid/legit/surface/none,
│   │                             材料按统一目录树取:报告/总报告.md·时间线·导图源·00_档案;
│   │                             口径版本 REV,增量落盘 成果评审.json —— legit=疑似正规,交人工复核)
│   ├── crypto/coppersmith.py     Coppersmith 小指数攻击(密码方向)
│   └── bin/timeout               macOS 上 `timeout` 命令的替身(本机系统没有)
│
├── tests/                        离线测试(不联网、不花模型 token)
│   ├── profile_test.py           断言:四包字段 + 黄金对照 + pentest 链路
│   ├── session_test.py           会话保护旋钮模拟(ADOPT_WORKERS 等)
│   ├── tsec_test.py              35 项断言:适配器/容器调度/多 flag/提交/轮换
│   ├── unit_test.py              单元测试
│   ├── scale_test.py             并发压测
│   ├── dashboard_check.py        控制台 smoke test
│   ├── fetch_challenges.py       拉平台题目列表做测试样本
│   ├── challenges.json           测试用题目样本
│   ├── local_contest.sh          本地 contest 模拟
│   ├── mock_tsec_server.py       TSec mock(适配器跑这个测)
│   ├── mock_contest_server.py    原 CTF 平台 mock
│   ├── mock_env.sh               mock 环境变量
│   ├── run_mock_e2e.sh           mock 端到端(6 题 9 flag)
│   ├── run_startsh_mock.sh        ./start.sh 入口的 mock 验收
│   ├── tsec_test.py              TSec 单元
│   ├── fake_pi/pi                假 pi CLI(不调真模型)
│   └── golden/prompt_{ctf,zhongce}.txt   黄金对照存档(改造前的 prompt 逐字保存)
│
│
├── projects/                     项目级工作区(./start.sh --project <名>)
│   └── 20260927-第一次测试/      → ../../20260927-第一次测试(符号链接,跨目录引用)
│
├── work/                         当前数据目录下,各题目/目标的工作目录(w p logs / NOTES.md / exp.py 等)
│
└── logs/                         当前数据目录下的运行日志
    ├── runner.out                调度主循环日志
    ├── submissions.log           每次提交与平台返回
    ├── findings.jsonl            正式渗透的 finding 原始记录
    ├── workers.json              在跑 worker 进程注册表(ADOPT_WORKERS 用)
    ├── supervisor.pid / runner.pid / stop.flag   进程控制信号
    └── *.json                    各事件追踪、session 历史
```

---

## 2. 数据流:从一个 prompt 到一次提交

```
                  ┌─────────────────┐
                  │  config.py      │  读 .env,同步 pi provider
                  └────────┬────────┘
                           ↓
                  ┌─────────────────┐
                  │  start.sh       │  VPN 预检 + 平台连通 + 控制台 + 后台
                  └────────┬────────┘
                           ↓ 启动
                  ┌─────────────────┐
                  │  runner.py      │  主循环:每轮 refresh → spawn → reap
                  └────────┬────────┘
                           ↓
                  ┌─────────────────┐
                  │  profile.current()│  决定用哪个 pack / adapter / 容器策略
                  └────────┬────────┘
                           ↓
              ┌────────────┼────────────┐
              ↓            ↓            ↓
       tsec_api /     contest_api /    targets_api /
       arena_api      (legacy)         (pentest)
              │            │            │
              └────────────┴────────────┘
                           ↓ 题目列表
                  ┌─────────────────┐
                  │  board.upsert   │  并入黑板(board.json, flock 锁)
                  └────────┬────────┘
                           ↓
                  ┌─────────────────┐
                  │  runner.rotate  │  时间片轮换(选最该跑的题)
                  │  runner.manage_containers │  关/开容器(3 个名额)
                  └────────┬────────┘
                           ↓
                  ┌─────────────────┐
                  │  runner.spawn   │  fork + exec,启动 pi 子进程
                  └────────┬────────┘
                           ↓
                  ┌─────────────────┐
                  │  solver.build_prompt │  替换 @@X@@ 占位符 → 完整提示词
                  └────────┬────────┘
                           ↓
                  ┌─────────────────┐
                  │  pi 子进程      │  LLM 自主循环:思考 → 工具 → 观察
                  │  (with --append-system-prompt <profile.system_prompt>)
                  └────────┬────────┘
                           ↓ 调用
              ┌────────────┼────────────┐
              ↓            ↓            ↓
       submit_flag    report_finding    tools/kb
       (ctf/zhongce)  (pentest)         (查知识库)
              │            │            │
              └────────────┴────────────┘
                           ↓
                  ┌─────────────────┐
                  │  board.locked   │  写回黑板(flag 命中 / finding 记录 / --done)
                  └────────┬────────┘
                           ↓
                  ┌─────────────────┐
                  │  runner.reap    │  收尾:状态机推进 + 释放 worker 注册表
                  └─────────────────┘
```

---

## 3. 关键文件:谁调用谁

| 上游文件 | 调用谁 | 干什么 |
|---|---|---|
| `start.sh` | `config.py --vpn` | VPN 预检 |
| `start.sh` | `python3 -c "..."` (内嵌) | 读项目级 `meta.json` 决定策略包 |
| `start.sh` | `dashboard.py` | 起网页控制台(后台) |
| `start.sh` | `runner.py` | 启动主循环,默认包在 supervisor 里(崩了自启) |
| `config.py` | `~/.pi/agent/models.json` | 同步 pi provider 字段 |
| `runner.py` | `profile.current()` | 拿当前 profile |
| `runner.py` | `tsec_api` / `newrank_api` / `contest_api` / `arena_api` / `targets_api` | 拉题 |
| `runner.py` | `board.upsert/upsert_question/flag_progress/add_fact` | 黑板读/写 |
| `runner.py` | `textutil.read_mixed()` + `sync_target_blocks()` | 黑板统一 UTF-8 + 维护「当前目标地址」块 |
| `runner.py` | `profile.stop_conditions/session_budget/is_pinned` | 任务驱动终止、分档预算、链式题钉死名额 |
| `runner.py` | `tsec_api.sync_containers(..., health=)` | 容器调度 + **服务级探活**(见 5.5c) |
| `runner.py` | `endgame_targets()` → `spawn.cap_for` | 收尾期自动增援(见 5.5d) |
| `runner.py` | `_nudge_by_progress()` + `solver.tool_signatures()` | Observer 的产出判据催促(见 5.5e) |
| `solver.py` / `dashboard.py` | `textutil.read_text_lossless()` | 读黑板(agent 写的内容可能是 GBK) |
| `runner.py` | `solver.spawn` | fork pi 子进程 |
| `runner.py` | `tools/submit_flag.py` / `tools/report_finding.py` | 给 worker 注入提交工具路径 |
| `solver.py` | `prompts/<pack>/prompt.md` + `prompts/<pack>/shared.md` + `prompts/<pack>/system.md`;另渲染 `@@SHARED_FACTS@@`(SHARED.md 尾部硬钉,含"实例已重置须复验"纪律) | 读提示词模板 |
| `solver.py` | `knowledge/<playbook>.md` | 读 playbook 节选 |
| `solver.py` | `tools/kb.py` | 注入知识库工具路径 |
| `solver.py` | `tools/pi_extensions/ai_trace.ts`(默认挂载) | 记录每次模型调用的起止/首包/usage → `<worker日志>.calls` |
| `dashboard.py` | `board.locked()` | 读黑板渲染看板 |
| `dashboard.py` | `logs/ai_usage.json`(`GET /api/ai_usage`) | AI 用量/计时顶栏指标 + 展开明细(由 ai_monitor 每 5s 落盘) |
| `dashboard.py` | `shezha_cluster_detail()` + 内嵌 PAGE | 涉诈簇关系图按连通分量分层，关系清单核对全部档案/AI 边；`#rotom?...` + `sessionStorage` 恢复刷新位置 |
| `dashboard.py` | 内嵌 PAGE 的节流/哈希守卫 | 滚动位置 200ms 节流写入、簇树内容哈希跳过重画、抽屉关着不渲染涉诈卡片、页面隐藏停轮询 |
| `dashboard.py` | `assets/markmap/render.js`(`markmapInto`) | 站点卡片用 markmap 官方 API 把 `思维导图源文件.md` 画成导图(容器 `#mm_svg`),默认宽度 420 / 关淡入动画;`_mindmap_localize` 另把「新窗口打开」那份 html 的 CDN 装载器换成本地库 |
| `dashboard.py` | 内嵌 PAGE 的 `.kv` / `.kv-wide` | 标签值容器:默认 `.kv` = 多列 auto-fit + 单行省略号(只给短标签值);装散文/文件清单的卡片套 `.kv-wide`(一行一条 + 换行),否则整行被截成「…」 |
| `dashboard.py` | `shezha_archive_add/unarchive/cluster/pending()` + `/api/shezha` 的 `archive*` 动作 | 涉诈「归档结案」:有结论的站退出工作视图且不再派发;真源 = `config.json` 的 `archive`(键=域名,值 `{at, reason}`),材料/评审原地保留、可撤销;三个批量入口带 confirm |
| `dashboard.py` | 内嵌 PAGE 的 `szOnlyArchived` / `szArcTag` / `szArcBtn` / `.sz-act` | 归档的界面层:左栏「已归档 N」chip(与战果/待审互斥,hash `arc=1`)、工作视图默认隐藏归档站、树行悬停归档/撤销、站点页归档卡、簇页「归档本簇」、待审「一键归档这 N 站」 |
| `dashboard.py` | `profile.names()` | 策略包下拉框 |
| `dashboard.py` | `config.write_env()` | 改 .env |
| `dashboard.py` | `assets/` 静态路由 | 托管字体(woff2,只读 assets/ 下文件) |
| `dashboard.py` | `python3 tools/submit_flag.py` | 提交按钮直接调 |
| `dashboard.py` | `python3 tools/report_finding.py` | pentest 收工按钮调 |
| `dashboard.py` | `tools/export_trace.py` | 审计导出按钮调 |
| `dashboard.py` | `tools/review_results.py` | 涉诈「AI 审成果」按钮(后台跑,增量重评;`_sz_pending` 复用其 `material_sig`/`REV` 算待办) |
| `dashboard.py` | 涉诈 `config.json stop_set` | 「停手」按钮把疑似正规站写进停打名单(派发侧唯一真源) |
| `tools/submit_flag.py` | `board.locked()` | 写黑板(flag 进度) |
| `tools/submit_flag.py` | `tsec_api.submit` / `newrank_api.submit` / `contest_api.submit` | 实际提交 |
| `tools/report_finding.py` | `board.locked()` | 写黑板(findings + delivered) |
| `tools/pentest_report.py` | `board.json` + `logs/findings.jsonl` | 聚合报告 |
| `docker/entrypoint.sh` | `tools/platform_env.py`(环境变量→.env、模型网关改写)→ `start.sh --check-only` → 前台 `runner.py` |
| `solver.py` | `in_container()` / `platform_note()` | 决定用 macOS 版还是容器版环境提示(见 5.5f) |
| `tools/kb.py` | `knowledge/.kb_index.json` + `knowledge/*.md` + `knowledge/vendor/` | 知识库 |
| `tools/gen_product_year.py` | `knowledge/wordlists/vendor-vocab.txt` | 产"产品词×年份"候选族(按指纹现场生成) |
| `tools/dict_prep.py` | `knowledge/wordlists/**` + 调 `tools/gen_product_year.py` | 组有序口令面(四段固定顺序) |
| `tools/submit_flag.py` | `logs/pending_flags.jsonl` | 平台没受理时把 (题号, flag) 记成待办 |
| `runner.maybe_backfill_flags()` | `tools/backfill_flags.py` | 平台可达时补交一轮(节流 `BACKFILL_INTERVAL`,0=关) |
| `tools/netcheck.py` | `tsec_api.list_questions` + `curl` + `requests` | 全量体检 |
| `tests/profile_test.py` | `solver.build_prompt` + `golden/*.txt` | 黄金对照 |
| `tests/tsec_test.py` | `tsec_api` + `mock_tsec_server` | 适配器 / 容器 / 提交 |
| `tests/session_test.py` | `runner` (子进程) | 会话保护旋钮 / 重启接管 / 前端 JS `node --check` |
| `tests/encoding_address_test.py` | `textutil` + `runner` | 黑板编码转码 / 容器地址变更与失效 |
| `tests/bench_loop_test.py` | `profile` + `runner` | 跑分循环:四种终止 / 分档预算 / 链式钉死 / 反作弊自检 |
| `tests/run_mock_e2e.sh` | `mock_tsec_server` + `fake_pi/pi` + `runner.py` | 端到端 |

---

## 4. 配置 / 状态文件:数据生命周期

| 文件 | 谁写 | 谁读 | 何时清 |
|---|---|---|---|
| `.env` | `config.py --xxx`、`dashboard.py` 表单、用户手改 | `config.read_env()`、`start.sh`(`set -a; . ./.env`) | 用户改或 `reset_env.py` 重置 |
| `~/.pi/agent/models.json` | `config.sync_provider()` | `pi --provider <name>` | 用户改或同步覆盖 |
| `board.json` | `runner.py`、`tools/submit_flag.py`、`tools/report_finding.py`(全部通过 `board.locked()` 写) | `runner.py`、`dashboard.py`、`tools/status_now.py`、`tools/postmortem.py` | `start.sh stop` 不会清;手动删则数据全丢 |
| `board.lock` | `board.locked()`(flock 文件锁) | 同上 | 不清 |
| `logs/runner.out` | supervisor 包着的 `runner.py` | `tail -f`、watchdog、运维 | `start.sh stop` 不会清;`watchdog.py` 按 `MAX_LOG_MB` 滚动 |
| `logs/submissions.log` | `tools/submit_flag.py` | 运维、复盘 | 同上 |
| `logs/findings.jsonl` | `tools/report_finding.py` | `tools/pentest_report.py` | 收工后可用 `tools/pentest_report.py --clean` 归档(默认不清) |
| `logs/workers.json` | `runner.py`(启动 worker 时注册,reap 时清掉) | `start.sh stop`(kill_registered_workers) | 自动维护 |
| `projects/<name>/meta.json` | `dashboard.py`「创建项目」对话框 | `start.sh`、`profile._project_profile()` | 删除项目时清 |
| `projects/<name>/targets.json` | 用户手填 | `targets_api`、`start.sh` 检查 | 用户改 |
| `work/<qid>/` | pi worker(写文件) | 后续 worker(读残留) | 用户清;runner 不会自动删 |
| `tools/bin/timeout` | 仓库自带 | 所有 worker | 仓库自带 |
| 涉诈:`<仓库>/00_总控/自动批次/成果评审.json` | `tools/review_results.py`(AI 判档,带 `rev` 口径版本章;材料=报告/总报告.md·时间线·导图源·00_档案,导图占位骨架不算)+ `dashboard.shezha_review_decide`(人工复核 `decision`) | `dashboard.py` 左栏/簇/单站视图、自动同步 `_sz_pending` | 删条目即重评;`rev` 旧条目会自动重读(deep/mid/legit) |
| 涉诈:`…/站点总库.json` | `dashboard.shezha_inventory`(1h 缓存;识别=`报告/总报告.md` 即站点,含无点簇级文件夹;字段=00_档案(新老两种格式)→ 缺则从总报告「当前结论/实名进展」派生;附导图元信息 `mm` 与根部零散文件 `loose`) | 左栏树/时间线/簇视图/单站页/导图路由 | 删了自动重建(零 AI) |
| 涉诈:`…/簇划分.json` | `dashboard.shezha_ai_cluster`(单次模型调用,增量并入) | 左栏簇树 | 删了下次自动重归簇 |
| 涉诈:`…/config.json` | 策略文件(人/`excel_targets.py`);`stop_set` 也可由控制台「停手」按钮追加 | `tools/excel_targets.py` 生成 targets 时排除 | 用户改 |

**铁律**:`.env` 与 `board.json` 是**有状态的**,删了等于从头来。其它日志类可以滚动/归档。

---

## 5. 状态机

### 5.1 题目状态机(`board.py`)

```
                  ┌─────────────┐
                  │  pending    │  拉题后默认;没人在跑
                  └──────┬──────┘
                         ↓ spawn
                  ┌─────────────┐
                  │  running    │  worker 启动;有轮转时间片
                  └──────┬──────┘
                         ↓ 提交 CORRECT / report_finding --done
                  ┌─────────────┐
                  │  solved     │  is_solved=True,runner 不再派人(平台记账)
                  └─────────────┘
                         ↘
                  (timeout / 超限) → failed → next_retry_at 后回 pending 重试
```

### 5.2 容器名额(`runner.py` `manage_containers`)

```
候选题池(全部 is_solved=False)→ 按优先级排序:
   ① 有 agent 在跑(hold_container)/ 在保护期 → 强制留
   ② 从没碰过(attempts=0) > 关卡低(level 小) > 已有进展(correct_flag_count 大)
选前 3 名 → 对它们确保 container_status=available
其余 → close
每轮重排(有进展的题会"续约"留在池里)
```

### 5.3 时间片轮换(`runner.py` `rotate`)

```
对每个占名额的题:
   - 若有 agent 在跑 且 ROTATE_WAIT_RUNNING=1 → 等它跑完
   - 若有 agent 在跑 且 ROTATE_WAIT_RUNNING=0 → 时间片到就杀(默认关)
   - 若 WORKER_MIN_LIFETIME 内 → 保护期内不轮换
   - 否则:已运行 > QUESTION_SLICE_SECONDS 且 本片内没有新 flag → 让位
让位的题 → close 容器、scheduler 重新选下一题
有新 flag → 续期(reset slice_start)
```

### 5.4 会话保护三件套(./start.sh 启动时打印 / [docs/PROFILES.md](./PROFILES.md) 有图)

| 旋钮 | 默认 | 行为 |
|---|---|---|
| `ADOPT_WORKERS=1` | 启 | runner 重启时**接管**仍在跑的 `pi` 进程(register 到新的 `workers.json`);`=0` 则 `start.sh stop` 旧的,start 时按注册表收 |
| `ROTATE_WAIT_RUNNING=1` | 启 | `manage_containers` 把"有 agent 在跑"标记为 hold → 不被轮换杀掉;`=0` 时间片到就杀 |
| `WORKER_MIN_LIFETIME=120` | 启 | 新 agent 启动后 120 秒保护期(此期间不轮换、优先占名额);`=0` 关 |

---

### 5.5 容器地址变更与黑板「当前目标地址」块(`runner.sync_target_blocks`)

```
容器重开 → IP/URL 变 → runner._accept_conn():
    旧地址进 board.dead_addrs,记 fact「容器地址变更」,并置 addr_changed_at
容器被释放/换走 → runner._mark_dead():当前地址立刻作废,connection 清空
每轮(sync_target_blocks,由 observe_shared 末尾调用):
    目标块内容签名变了才重写 SHARED.md(地址没变不动文件)
    工作区还没建好时写失败 → 不记签名,下一轮补写
写进黑板的形式(块插在「## 已确认的事实」之前,行前缀 >> [harness],每次重写时先剔除再重建):
    >> [harness] 当前目标地址(harness 维护,**以此为准**;黑板上其它地址可能是旧容器的残留):
    >> [harness] 当前目标地址:url1=http://10.0.174.186:80
    >> [harness] 容器状态:available
    >> [harness] 已失效地址(打这些是浪费,别再试):http://10.0.174.184:80 · http://10.0.174.185:80
```

编码:黑板是 agent 用 shell 写的,可能混进 GBK(实测 0xbc)。`textutil` 逐行解码(UTF-8 → GB18030 → 兜底),
保住内容并把文件统一成 UTF-8;检测到转码时会在黑板里留一条 Observer 提醒(教它用 `iconv` / `printf`)。
worker 进程另外注入 `LC_ALL/LANG=en_US.UTF-8`、`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`,从源头少产 GBK。

---

### 5.5b 提交假阴性重试(`tsec_api.submit_with_retry`)

```
提交 → 受理但判 false? → 同串重试(FALSE_NEGATIVE_RETRIES=2,间隔 3s/5s)→ 仍 false 才如实报 WRONG
        └ 只重试**完全相同**的字符串:换变体=猜 flag(prompt 明令禁止)→ 真错不浪费额度超过 2 次
resp 里带 _false_negative_recovered(第几次救回)/_false_negative_checked(试了几次),便于复盘
```

### 5.5c 容器服务级自愈(`tsec_api._health_tick`,策略包 `containers.health`)

```
每轮 sync_containers(…, health=) 里顺带做一步探活(默认 90s 一次):
    _probe_alive(addr) = TCP 通 且(HTTP 端口/always)任何 HTTP 响应
        └ 只探 TCP 会漏掉"内核 backlog 收下 SYN、应用层永不回"的假死(实测 c-02 就是这样)
    连续 fail_threshold(3)次判死 → close_async(强制关掉) → 下一轮 sync 自动 start 新容器
        └ 地址变更照旧走 runner._accept_conn / dead_addrs / 黑板"当前目标地址"块
    封顶 max_heal_per_question(3)+ 冷却 heal_cooldown_s:防"agent 反复打死容器 → harness 反复换"
为什么要它:平台 container_status 只说明"资源占着";c-02 那次平台一直报 available,
而 harness 又因 `unsolved <= MAX_ACTIVE_CHALLENGES` 不做轮换 → 死容器永远换不掉,只能人工救。
```

### 5.5d 收尾期自动增援(`runner.endgame_targets`)

```
每轮 spawn() 先算一次"该给谁加人":
    未解题 ≤ unsolved_lte(2)  且  有空闲并发位  且  该题在跑且距上次实质进展 > after_running_s(600s)
        → 同题上限放到 max_helpers,新 worker 带 dispatch.endgame.brief 当分工简报
实测来源:第四次跑分全场只剩一题时,5 个并发位空着 4 个,增援是收工前 14 分钟人工加的。
```

### 5.5e Observer 催促判据(`runner._nudge_by_progress` / `runner.OBS_MODE`)

```
steps 模式(ctf 保留):每 KB_NUDGE_STEPS 步没提交 → 催一次查知识库(短周期比赛够用)
progress 模式(zhongce/pentest):只在两种信号下催,且边沿触发 + 冷却 + 每题上限
    ① 重复命令:solver.tool_signatures() 最近 40 次里"完全相同的 (工具,参数)" ≥ repeat_sig
       (字节级比较 —— id=1/2/3 这类正常探测不误伤;这是唯一被实测验证过的"真卡住"信号)
    ② 长期无进展:距上次实质进展(新 flag / 黑板出现新的 agent 结论)超过 stall_s
    容器不可用时**一律不催**(那是环境问题,该换容器,不是催 agent 换思路)
```

### 5.5f 平台差异(托管容器 vs macOS)

```
solver.in_container() = ROTOM_HOSTED=1 或存在 /.dockerenv
  ├─ 提示词 @@PLATFORM_NOTE@@ / @@PLATFORM_TOOLS@@:macOS 渲染为空串(基线逐字不变),容器里说明
  │   "Linux amd64 原生可执行 / timeout 是真的 / orb 是兼容层 / 无外网先查知识库"
  └─ 环境速查两套:*_ENV_HINT(macOS:orb -m pwn64、nmap 无 root) / *_ENV_HINT_HOSTED(容器:工具本地全有)
tools/bin/orb:兼容层,`orb -m <vm> <cmd>` → 本地执行(容器里没有 OrbStack 虚拟机)
tools/platform_env.py:环境变量→.env;模型地址按平台规则改写 <域名>.tsecbench.gw(https→http)
```

### 5.6 循环形状(跑分特化:`runner.STOP_MODE` / `profile.loop_mode`)

```
终止条件(策略包 loop.stop 声明;众测=四种,正式渗透=两种):
    goal_done      全部完成(平台口径 is_completed / pentest 全部 --done)
    platform_dead  平台不可恢复(invalid_state 非 max-active / 任务已结束) → 收工 + 导出
    net_gone       网络/平台连续失败 ≥ NET_GONE_STREAK(默认 3,通了清零)
    token_bad      token 无效(读取 message 判别,不只看 code)
    (人工 stop 记 manual;window_end 仅 window 模式)
落盘:logs/run_stop.json{reason, detail, stop_mode, allowed, solved/flags/findings 快照}

模式:loop.mode = work(任务驱动,**window_closing() 恒 False,时间只用于预算/节奏**)
               | window(比赛制:到点停止派题并收工)
预算:loop.prompt_time_budget 决定是否给 worker 灌"剩余时间"(跑分包 false → prompt 里没有时间压力)
时间:budgets.by_difficulty{easy/medium/hard} + budgets.chain{flag_count_gte, session_s, pin_slot}
      → runner.worker_timeout_for(attempts, entry) 按 entry 取值(没配的包回退老逻辑)
钉死:budgets.chain.pin_slot=true 且 flag_count>=flag_count_gte → rotate() 跳过、hold_container 恒真
```

## 6. 旋钮全表

### 6.1 策略包决定(`profiles/*.json`)

| 字段 | 含义 | ctf | zhongce | pentest |
|---|---|---|---|---|
| `goal` | 目标与记账方式 | flags | flags | findings |
| `task_source` | 任务源(空=平台适配器) | (空) | (空) | `targets_api` |
| `prompt_pack` | 提示词包目录 | ctf | zhongce | pentest |
| `connection_style` | 任务消息里连接信息格式 | ctf | container | target |
| `default_env_hint` | 默认追加的 playbook 名 | none | pentest | pentest |
| `dispatch.workers_per_question` | 同题默认 agent 数 | 2 | 2 | 1 |
| `dispatch.max_helpers` | 增援上限 | 4 | 3 | 2 |
| `dispatch.endgame` | 收尾期自动增援(只剩少量题 + 有空闲并发 → 给卡住的题加派补位 agent,带分工简报) | (无) | `{enabled, unsolved_lte, after_running_s, brief}` | (无) |
| `observer` | 催促判据:`steps`=每 N 步催一次查知识库;`progress`=重复命令 or 长期无新证据才催(边沿触发+冷却+上限) | `mode=steps` | `mode=progress` | `mode=progress` |
| `containers.manage` | harness 是否管容器 | false | true | false |
| `containers.rotate` | 是否时间片轮换 | false | true | false |
| `containers.health` | **服务级探活**(平台报 available ≠ 服务活着):连续 N 次探不通 → 强制关掉换新容器 | (无) | `{enabled, probe_interval_s, fail_threshold, http_check, max_heal_per_question, heal_cooldown_s}` | (无) |
| `containers.vpn_gate` | 是否要 VPN 预检 | false | false | true |
| `system_prompt` | pi 系统提示词文件 | shared/system-ctf.md | shared/system-ctf.md | pentest/system.md |
| `playbooks` | 类别 → playbook 文件名映射 | 6 个方向 + 中文别名 | 6 个方向 + 中文别名 | pentest-formal + 6 个方向 |

### 6.2 运行时旋钮(.env / 网页控制台)

| 类别 | 旋钮 |
|---|---|
| 策略 | `PROFILE` |
| 平台 | `CONTEST_ADAPTER`、`BENCHMARK_BASE_URL`、`BENCHMARK_TOKEN`、`TEAM_TOKEN`、`VPN_CHECK_URL`、`VPN_OVPN_FILE` |
| 模型 | `PI_PROVIDER`、`PI_BASE_URL`、`PI_API_KEY`、`PI_MODEL`、`PI_API_TYPE`、`PI_API_KEY_VAR` |
| 容器 | `MAX_ACTIVE_CHALLENGES`、`QUESTION_SLICE_SECONDS`、`ROTATE_COOLDOWN`、`MIN_QUESTION_SLICE`、`HARD_SLICE_FACTOR` |
| 并发 | `START_WORKERS`、`MIN_WORKERS`、`WORKERS_PER_QUESTION`、`MAX_HELPERS_PER_QUESTION` |
| 超时 | `WORKER_TIMEOUT`、`TIMEOUT_ESCALATE`、`MAX_WORKER_TIMEOUT`、`MAX_ATTEMPTS`、`CONTAINER_START_TIMEOUT`、`CONTAINER_CLOSE_TIMEOUT` |
| 重试节奏 | `QUICK_RETRY_DELAY`、`FAILED_RETRY_AFTER`、`POLL_INTERVAL` |
| 知识库 | `KB_NUDGE_STEPS`、`KB_AUTO_SEARCH` |
| 会话保护 | `ADOPT_WORKERS`、`ROTATE_WAIT_RUNNING`、`WORKER_MIN_LIFETIME`、`SESSION_SLICE_SECONDS`(短会话接力) |
| 故障告警 | `API_FAIL_RELOG_S`、`STALL_WARN_S`、`STALL_MIN_PENDING`、`GW_PROBE_INTERVAL`、`GW_SLOW_FACTOR`(模型延迟基线) |
| 总时长 | `ROUND_WINDOW_MINUTES`、`ROUND_END_AT`、`STOP_SPAWN_BEFORE_END` |
| 提示 | `HINT_AFTER_ATTEMPTS`(0=默认·agent 自主)、`HINT_EARLY_DIFFS`、`HINT_SKIP_DIFFS`(自动注入模式分档;agent 自取走 `tools/get_hint.py`;空结果不落缓存,下次尝试重取) |
| 提交 | `FALSE_NEGATIVE_RETRIES`(平台假阴性同串重试次数,默认 2) |
| 故障告警 | `API_FAIL_RELOG_S`、`STALL_WARN_S`、`STALL_MIN_PENDING`(2026-10-25:连败必报/停滞必告) |
| 其它 | `KEEP_RUNNING`、`MAX_LOG_MB`、`QUOTA_BACKOFF`、`NET_PROXY`、`NET_ALLOW`、`OBSERVER_MIN_SECONDS`、`OBSERVER_KEEP_LINES`、`IGNORE_SOLVED` |

完整字段含义与默认值见 `.env.example` 与 `config.py DEFAULTS`。

---

## 7. 扩展点

### 7.1 加一个策略包

例如"内网横向"(独立策略):

1. 复制最接近的 profile:`cp profiles/pentest.json profiles/lateral.json`,改:
   - `name: lateral`
   - `goal: findings`
   - `prompt_pack: lateral`
   - `playbooks.pentest: lateral.md`(在 knowledge/ 下加一份)
2. 复制提示词包:`cp -r prompts/pentest prompts/lateral`,改 `prompt.md` / `shared.md` / `system.md` 文案
3. 写一个对应的 playbook:`knowledge/lateral.md`
4. 加测试断言:在 `tests/profile_test.py` 的"策略包本身"段加一行
5. **更新 [../README.md](../README.md)、本文件**(版本演进记进 git 提交信息)

### 7.2 加一个平台适配器

例如新平台 Xyz:

1. 在仓库根写一个 `xyz_api.py`,模仿 `tsec_api.py`(实现 `list_questions/start_container/submit_flag/close_container/get_hint`)
2. `runner.py` 里加一个分支:`elif CONTEST_ADAPTER == "xyz": import xyz_api as api`
3. `.env.example` 加 `CONTEST_ADAPTER=xyz` 的注释行;`config.py DEFAULTS` 不需要改
4. `dashboard.py` 如果有「平台类型」下拉框,加上 xyz
5. `tests/` 加 `mock_xyz_server.py` + `xyz_test.py`
6. **更新 changelog + README + 本文件**

### 7.3 加一个工具

给 worker 用:

1. 写 `tools/<name>.py`,argparse 完整,退出码清晰(0 成功 / 非 0 失败)
2. 提示词模板里用 `@@<NAME>_TOOL@@` 占位符
3. `solver.py` 的 `values` 字典加键(参见 `prompts/<包>/prompt.md` 的可用占位符表)
4. **更新提示词文档说明 + changelog**

### 7.4 加一个 prompt 占位符

1. `prompts/<包>/prompt.md` 加 `@@NEW@@`
2. `solver.py` 的 `values` 字典加 `'@@NEW@@': ...`
3. `tests/golden/prompt_{ctf,zhongce}.txt` 用新占位符值替换后重生成(只有要改基线时才需要)
4. **更新 changelog**

---

## 8. 排障索引(从症状找文件)

| 现象 | 先看 |
|---|---|
| runner 没起 | `logs/runner.out` 最后 50 行、`logs/supervisor.pid` 是否还在 |
| VPN 连不上 | `python3 tools/netcheck.py`;`.env` 里 `VPN_CHECK_URL` 与 `VPN_OVPN_FILE` |
| 平台 token 报错 | `python3 config.py --benchmark-token <UUID>`;`dashboard.py` 改完保存会写 `.env` |
| 模型 401 / Connection error | `NET_PROXY=direct` 是否被设;VPN 是否走了全隧道;`pi --provider` 是否能直连 |
| 容器一直在转圈 | `logs/runner.out` 的「容器就绪 / 轮换」段;`manage_containers` 名额档是否冲突 |
| 一题多 flag 卡住 | `board.json` 里这题的 `correct_flag_count` / `found_flags`;提示词里"继续找下一个漏洞"是否被忽略 |
| 正式渗透 agent 拒绝干活 | 看 `prompts/pentest/system.md` 授权声明是否被吃;`tools/report_finding.py --help` 看用法 |
| 黑板出现乱码 / 编码报错 | `textutil.py`(逐行转码,不丢内容);`runner.out` 里搜「混进了非 UTF-8」;提示词里"写黑板统一 UTF-8"硬规则 |
| agent 反复打同一个死地址 | 黑板顶部 `>> [harness]` 块;`board.json` 里该题 `dead_addrs`;`runner._accept_conn` / `_mark_dead` |
| 正确 flag 被判错 | 平台假阴性:harness 已同串重试(`FALSE_NEGATIVE_RETRIES`,默认 2);仍错就是真错(content 从目标重新提取) |
| agent 自己的 python 脚本报 502 | macOS 系统代理:direct 模式已注入 `no_proxy=*`;自己写脚本也可 `trust_env=False` |
| 想复用上一轮结论 | `profiles/zhongce.json` 的 `kb_extra` 填上一轮 `solve/`/`ref/` 目录 → worker 里 `kb.py search` 直接能查到(第四次跑分已填入 `20260929-第四次测试/kb_extra/{solve,ref}`,复制时脱敏了旧 flag 串、跳过 18MB 资源文件) |
| 跑分跑着跑着停了 | `logs/run_stop.json`(终止原因码 + 快照);四种之外的原因说明有人改了策略包 |
| 链式题老是重做第一层 | 该题是否被钉住(`board.json` 的 `flag_count` + `profiles/zhongce.json` 的 `budgets.chain`);`logs/runner.out` 搜「钉住名额」 |
| agent 说"时间过半换攻击面" | 跑分包应当**不灌时间预算**:查 `profiles/*.json` 的 `loop.prompt_time_budget` |
| 容器"看着开着"但打不通(app 死了) | `logs/runner.out` 搜「服务探活」;策略包 `containers.health` 是否开(zhongce 默认开);探活判死会自动换容器,日志里会写「强制换容器(第 n/N 次)」 |
| 只剩最后一题、并发位空着却没人帮它 | 策略包 `dispatch.endgame`;`runner.out` 搜「收尾期:…自动加派」 |
| 黑板被"已 N 步未提交"刷屏 | `profiles/*.json` 的 `observer.mode`:众测/渗透应为 `progress`(只看重复命令与长期无新证据);ctf 才是 `steps` |
| 想让 agent 去查某个组件的洞 | `python3 tools/kb.py hit "comfyui"` / `hit "grafana 8.2"` / `hit "hugegraph"` / `hit "jwt 混淆"` → `knowledge/cves/` 速查卡(卡内 CVE 表按 `✅实测/📋公告/⚠候选` 分档);通用方法论见 `kb.py show cves/find-cve` |
| 打包成托管镜像 | `bash docker/build.sh`(构建期 selfcheck 不过就不出镜像);环境变量清单与无外网审计见 `docs/DOCKER.md` |
| 容器里 agent 按 macOS 那套绕 | `solver.in_container()`:`ROTOM_HOSTED=1` 时应拿到 HOSTED 版提示;`tools/bin/orb` 是兼容层 |
| 测试跑挂了 | `tests/profile_test.py` 先跑(它覆盖最广) |
| 改完策略包提示词 | `python3 tests/profile_test.py` 看黄金对照是否还过;不通过说明动了不该动的地方 |
| 想回滚 | 改造前的版本在 git 历史里:`git log --oneline` 找到提交后 `git checkout <提交> -- <文件>` |
| 找不到自己的项目 | `projects/<名>/` 是不是符号链接;`./start.sh status --project <名>` 看 banner |
| 控制台样式变丑/字体不对 | 用 git 历史回退 `dashboard.py`;字体加载看 `/assets/fonts/` 是否 200 |
| 卡片里长文被截成「…」 | 该值容器要套 `class="kv kv-wide"`(一行一条 + 换行);默认 `.kv` 是多列 auto-fit + 单行省略号,只适合难度/分值这类短值 |
| 改了 `dashboard.py` 但页面没变 | 控制台把 PAGE 读进内存了,**必须重启进程**;核对端口上的版本:`curl -s http://127.0.0.1:8799/ \| grep -c 'kv-wide'`,别只看文件时间 |
| 侧栏层级 | 只读状态在顶栏(进度/并发/剩余),操作在侧栏底部两行(跑控 + 抽屉入口);id 不变,JS 不用动 |
| 设置抽屉层级 | 两页:常用(平台/模型/项目)/ 高级(运行参数);切换用 `cfgView` 切 `hidden`(不是 style.display) |

---

## 9. 与上游 Rotom 的边界

| 模块 | 本分支 | 上游 Rotom |
|---|---|---|
| 黑板(board.py) | 沿用 + 加 findings 字段 | 原版 |
| 调度(runner.py) | 沿用 + 加 vpn_gate/rotate/manage_containers | 原版 |
| 提示词(solver.py) | 沿用 + 加 profile 切换 + system_prompt 注入 | 原版 |
| 控制台(dashboard.py) | 沿用 + 加策略包 / 项目管理 / pentest 按钮 | 原版 |
| 知识库(tools/kb.py) | 沿用 | 原版 |
| 审计(tools/export_trace.py) | 沿用 | 原版 |
| 平台适配器 | tsec_api.py + targets_api.py | contest_api.py + arena_api.py |
| 策略包(profile.py + profiles/ + prompts/) | 新增 | 无 |
| 正式渗透(tools/report_finding.py + tools/pentest_report.py) | 新增 | 无 |
| 黄金对照(tests/golden/) | 新增 | 无 |

机制层(黑板 + 调度 + 提示词 + 控制台 + 知识库 + 审计)完全沿用;
策略层(策略包 + 三套提示词 + 正式渗透工具链)是本分支新增的核心;
宿主层(TSec 适配器 + targets_api)是本分支替换的部分。