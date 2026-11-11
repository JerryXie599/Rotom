# 托管模式打包(Docker · TSec Benchmark)

> 目标:把 Rotom 打成一个**起容器就自己开始解题**的镜像上传到平台托管运行。
> 两条硬约束:**① 容器内无公网**;**② 不得内置"针对题目的历史答题记忆"**(否则按作弊处置)。

## 1. 一分钟上手

```bash
bash docker/build.sh                 # 构建 linux/amd64 镜像 → dist/rotom-agent.tar.gz(构建机需要联网)
# 平台上传页 →「制作并上传 Docker 镜像」→ 选 dist/rotom-agent.tar.gz
# 运行环境变量(上传页填,见 §4):PI_UPSTREAM_BASE_URL / PI_API_KEY / PI_MODEL
```

镜像入口 `docker/entrypoint.sh` 会自动:生成 `.env`(把模型地址改写成平台网关)→ 配置体检 →
前台跑 runner(崩溃自动重启)→ 出现终态终止原因(`goal_done/platform_dead/token_bad`)就退出容器。

## 2. 镜像里有什么(离线自足)

| 类别 | 内容 |
|---|---|
| 运行时 | Python 3.12(requests / pwntools / pycryptodome / z3 / sympy / PIL / numpy…)、Node 22 + **pi CLI**、tini |
| 侦察/利用 | nmap、**nuclei(+13600 模板)**、dirsearch、gobuster、ffuf、nikto、whatweb、sqlmap、mysql/psql/redis 客户端、smbclient |
| 二进制/逆向 | gdb、binutils、strace/ltrace、**radare2**、patchelf、ROPgadget/pwntools、gcc/g++/make、openjdk-17(含 javac)、php-cli |
| 取证/密码 | tshark、tcpdump、binwalk、exiftool、steghide、foremost、sleuthkit、hashcat、john、字典(dirb/dirbuster + rockyou 可选) |
| 离线知识 | `knowledge/`(playbook + 技术卡 `cards/` + CVE 速查 `cves/`)、`knowledge/vendor/`(PayloadsAllTheThings、RsaCtfTool) |
| 兼容层 | `tools/bin/orb` —— 把 macOS 时代的 `orb -m pwn64 ...` 写法直接映射成本地执行,老文档/老提示词不用改 |

## 3. 无外网审计(逐项)

| 原来会联网的地方 | 处理 |
|---|---|
| 大模型 API(`PI_BASE_URL`) | 入口脚本按平台规则改写成 `http://<域名>.tsecbench.gw/...`(`tools/platform_env.py gw_rewrite`) |
| nuclei 模板(默认要 `nuclei -update-templates`) | **构建期**预置 `~/nuclei-templates` → 镜像 `/root/nuclei-templates`。⚠ 必须 `COPY --from=appsrc`(**已脱敏树**,不是构建上下文):宿主机模板目录带着 nuclei 索引缓存(记录模板的绝对来源路径,实测 2.7 万处宿主家目录路径)与上游示例串,阶段 1 的 sanitize 会清掉它们 —— 2026-10-19 逐层审计抓到过"直拷上下文绕过脱敏"这条,已修 |
| ffuf / nuclei / ysoserial / jdwp-shellifier 二进制 | **构建期**从各自官方 release 下载进镜像(Dockerfile 里逐条可见) |
| 字典(rockyou 等) | 构建期下载;apt `wordlists` 已含 dirb/dirbuster 列表 |
| PayloadsAllTheThings / RsaCtfTool | 已在 `knowledge/vendor/`(公开资料,随镜像) |
| `fetch_vendor.sh` / `vpn.sh` / `netcheck.py` | 开发期工具,运行不需要;`knowledge/fetch_vendor.sh` 不进镜像 |
| 目标容器里的 `pip install` / `apt install` | 默认无外网,装了也白等 —— 提示词已明写"别装,用现有工具/库" |

**审计命令**(镜像内,离线可跑):
```bash
bash docker/selfcheck.sh          # 工具链 + 知识库 + 合规扫描
grep -rn "http://\|https://" --include=*.py tools/ config.py solver.py runner.py | grep -v tsecbench.gw
```

## 3.5 构建期下载源(只在构建时用,运行期无关)

本机访问 Docker Hub 不通、`deb.debian.org` / `nodejs.org` 会卡住,所以构建期统一走国内镜像:

| 用途 | 源 |
|---|---|
| 基础镜像 | `docker.m.daocloud.io/library/python:3.12-slim-bookworm`(官方镜像副本;`--build-arg BASE_IMAGE=...` 可切回) |
| apt 包 | `mirrors.tencent.com/debian`(同一批官方 deb) |
| pip 包 | `mirrors.tencent.com/pypi/simple` |
| Node 二进制 / npm | `cdn.npmmirror.com/binaries/node` / `registry.npmmirror.com` |
| nuclei / radare2 / nikto / ysoserial / 字典 | 各自官方 release(GitHub) |

所有下载都带 `-4 --connect-timeout 20 --retry 3`(实测容器里 IPv6 先试会挂住)。
**这些源只影响"从哪儿下载";装进镜像的是同一批官方包**,运行期完全不联网。

## 4. 运行时环境变量

| 变量 | 谁填 | 说明 |
|---|---|---|
| `BENCHMARK_TOKEN` / `BENCHMARK_BASE_URL` | **平台自动注入** | 答题 API 凭证;程序内部照常用(与本地模式一致) |
| `PI_UPSTREAM_BASE_URL` | **你填** | 你原本要用的模型 API(白名单内),如 `https://api.deepseek.com/v1`;入口脚本自动改写成 `http://api.deepseek.com.tsecbench.gw/v1` |
| `PI_API_KEY` | **你填** | 该模型 API 的 key(只从环境变量读,**不进镜像**) |
| `PI_MODEL` | **你填** | 模型名,如 `deepseek-chat` |
| `PI_BASE_URL` | 可选 | 已给则**原样使用**(本地模式用它指向自建网关时方便) |
| `PI_PROVIDER` / `PI_API_TYPE` | 可选 | 默认 `rotom` / `openai-completions` |
| `START_WORKERS` / `MAX_ACTIVE_CHALLENGES` | 可选 | 并发与容器名额;默认 5 / 3 |
| `ROUND_END_AT` | 可选 | 本地显示用(任务驱动模式下不参与终止判定) |

> 模型 API 必须在平台的《大模型 API 白名单》里;白名单之外(含自建网关)在托管环境**不可达**。

## 5. 合规:为什么不含"历史答题记忆"

| 风险物 | 处理 | 复核 |
|---|---|---|
| `profiles/zhongce.json` 的 `kb_extra`(跨轮战果目录) | 构建时**清空** | `docker/selfcheck.sh` 断言为空 |
| `logs/` `work/` `projects/` `traces/` `board.json` | `.dockerignore` 排除 | selfcheck 断言不存在/为空 |
| `.env`(明文 token/key) | 排除;容器启动时由环境变量生成 | selfcheck 扫描明文凭据 |
| `tests/`(mock 平台的假 flag 夹具) | 排除 | selfcheck 扫 flag 样式字符串 |
| 知识库 `cards/` `cves/` | **保留** —— 全是公开技术知识(组件/端口/CVE 通告 → 验证方法),无题号、无答案;`tests/bench_loop_test.py` 的反投机断言会持续守这条线 | `python3 -m pytest` 或 `python3 tests/bench_loop_test.py` |

## 5.5 镜像内脱敏(平台会审计容器数据)

`docker/sanitize.sh` 在构建期(COPY 之后)跑一次,逐条打印改了什么,`selfcheck.sh` 再硬断言一遍:

| 类别 | 处理 |
|---|---|
| 第①类 身份类措辞(检测规则见 `docker/selfcheck.sh`) | 授权话术只保留中性表述(授权范围来自任务消息、非破坏、数据最小化) |
| 联系方式(邮箱/QQ/手机号) | 替换为"通过项目主页联系"一类中性文案 |
| 第②类:宿主机用户主目录路径 | 统一替换成 `<工作目录>`(注释与文档一起清) |
| 自建/实验地址(自建网关、实验网段、VPN 客户端地址、现场端点、平台任务 ID) | 替换成占位符 |
| 跨轮战果目录 `kb_extra` | 清空(平台明令:不得内置针对题目的历史答题记忆) |

> 仓库本体(本机开发用)不做这些替换 —— 只处理**镜像内**的副本,本地工作流不受影响。

**为什么用多阶段构建**:如果"先 COPY 原始文件、再在镜像里覆盖一层脱敏",敏感文本仍留在**底层 layer** 里
(`docker save` 出来的 tar 能直接扒出来:实测逐层命中 5.5 万处宿主机路径模式)——所以脱敏放在**独立的第一阶段**,最终镜像只从那一阶段拷"已清干净的树",
原始文本不进任何一层。另:apt 装的上游文档/示例(nmap 默认账号指纹表、Ruby/Thor 文档里的示例路径)必须在
**apt 那一层里就删/替换**——换个层再删只是"盖住",原字节还在。交付前用脚本逐层全文核过
(`docker save` → 解 tar → 每个 blob 搜敏感串)。规则表(与命中样本)只在第一阶段的 `docker/sanitize.sh` 里,
该脚本随后被删除 —— 所以镜像内的 `docker/selfcheck.sh` 不再携带任何敏感字面量。

## 6. 上传前的本地验证(平台要求先做本地模式评测)

1. **本地模式**(不进 Docker):`./start.sh --project <名>`(用真实 token 与直连模型)跑通一遍 —— 这一步就是平台说的"先本地模式评测"。
2. **镜像自检**:`bash docker/build.sh`(末尾会自动跑 selfcheck)。
3. **容器冒烟**(可选,验证"起来就自跑"):
   ```bash
   docker run --rm -it --platform linux/amd64 \
     -e PI_UPSTREAM_BASE_URL=https://api.deepseek.com/v1 -e PI_API_KEY=sk-xxx -e PI_MODEL=deepseek-chat \
     rotom-agent:latest
   ```

### 6.1 本地整轮容器模式(不进平台托管、就在本机跑一整轮)

复刻托管环境的硬约束:**哈希锁内自带 openvpn 让容器自己连靶场 VPN** + **出网锁**(只放行靶场/局域网 +
模型网关 + 平台域名 + DNS)。这是"上传平台之前最接近托管真实条件"的一次真跑分,也是唯一能在一轮内
验证"镜像里那些装备 agent 会不会主动用"的方式。

```bash
RUN_DIR=/path/to/round-dir          # 本轮数据目录(每轮换新,别复用上一轮的 board.json/logs)
OVPN=/path/to/task_xxx.ovpn         # 平台创建跑分任务时下发的那个文件

docker run -d --name rotom-rN --platform linux/amd64 \
  --cap-add=CAP_NET_ADMIN \
  -e ROTOM_RUN_DIR=/rotrun -e PROFILE=zhongce \
  -e BENCHMARK_TOKEN=<平台下发的 UUID> \
  -e BENCHMARK_BASE_URL=https://tsecbench.zc.tencent.com \
  -e VPN_AUTOCONNECT=1 -e VPN_OVPN_FILE=/ovpn/task.ovpn -e VPN_CHECK_URL=http://10.0.100.58 \
  -e EGRESS_LOCKDOWN=1 \
  -e PI_BASE_URL=<模型地址>/v1 -e PI_API_KEY=<key> -e PI_MODEL=<模型名> \
  -v "$RUN_DIR":/rotrun \
  -v "$OVPN":/ovpn/task.ovpn:ro \
  rotom-agent:latest

docker logs -f rotom-rN             # 看启动与调度;收工后容器自己退出(不空转重启)
```

要点(都是实测踩出来的):

- `--cap-add=CAP_NET_ADMIN` 是必需的(iptables 出网锁 + openvpn 都要)。
- **`PI_BASE_URL` 直填**(本地自建网关走这个);只有 `PI_UPSTREAM_BASE_URL` 会被改写成平台网关
  `<域名>.tsecbench.gw` —— 托管上传时用后者,本机跑用前者。
- **每轮换新的 `RUN_DIR`**:复用上一轮目录会带着它的 `board.json`(已解题目、`found_flags`)进场。
- VPN 没连上时容器会等一段再继续(靶场不通会导致查题失败),日志里会写明,不是死锁。
- 平台返回 `task ... already finished` / `token 无效` → 落 `platform_dead`/`token_bad` 后**容器自己退出**,
  这是"任务已结束"的正确终态,不是崩溃。

**彩排(冒烟)**:想在不消耗跑分任务的前提下验证整条链路,可以拿**上一轮的旧 token + 旧 ovpn** 起一次 ——
预期结果是:配置节打印正确(`CONTEST_ADAPTER=tsec`、`PI_BASE_URL` 未被改写、`pi provider 同步:成功`)、
模型网关在出网锁下可达、runner 起来后平台回 `task ... already finished` 并干净退出。
除了"新任务的 token/ovpn",这一趟就把全部接线都验了。
   本地没有平台网关,冒烟时可加 `-e PI_BASE_URL=http://<你的本地网关>/v1` 走直连。

## 7. 已知限制

- 平台上传上限 **3GB**,`docker/build.sh` 会打印镜像体积(当前约 2.x GB);
- 模型网关只放行白名单域名,自建/中转网关在托管环境不可用;
- 靶场容器由平台管理,镜像里只跑 harness 与 agent,不碰平台侧;
- 容器一次性:每次任务从零开始(这正是不让带历史记忆的另一面)。