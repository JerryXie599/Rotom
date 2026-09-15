"""Solver worker:为一道题目准备隔离工作区,并启动 pi 进程自主解题。

参考 Cairn 的设计:
  - 每题一个隔离 workspace(类似 Cairn 的 per-project worker container)
  - 同一题可有多个 worker(不同 slot),通过 SHARED.md 共享黑板互相感知(Stigmergy)
  - prompt 采用 origin/goal 结构(类似 Cairn 的 bootstrap 任务)

目录布局:
  work/<qid>_<title>/
    SHARED.md          # 本题共享黑板,所有 agent 读写
    files/             # 附件解压(所有 agent 只读引用)
    w1/  w2/  ...      # 各 worker 的独立工作目录(pi 的 cwd)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path

import threading

import config as config_mod

ROOT = Path(__file__).resolve().parent          # 代码位置(工具脚本在 ROOT/tools 下)
RUN_DIR = Path(os.environ.get("WQH_RUN_DIR") or ROOT)  # 本项目的数据目录
WORK_DIR = RUN_DIR / "work"
LOG_DIR = RUN_DIR / "logs"
SUBMIT_TOOL = ROOT / "tools" / "submit_flag.py"
RESET_TOOL = ROOT / "tools" / "reset_env.py"

# 多 agent 同题时的分工,按 slot 区分
ANGLES = {
    1: "主攻最短、最可能的利用路径(先拿最容易的突破点)",
    2: "走与其它 agent 不同的攻击面:换端点/换漏洞类型/换工具链,严禁重复别人已试过且失败的思路",
    3: "交叉验证与补漏:复核其它 agent 的结论与中间产物,重点排查他们忽略的细节和已知写法的变种",
}

SHARED_HEADER = """# 本题共享黑板(SHARED)

多个 agent 正在同时解这道题,本文件是你们唯一的沟通渠道(黑板上写事实,不写猜测)。

规则:
1. 动手前先读完本文件,明确别人已经确认了什么、正在尝试什么,严禁重复已失败的思路。
2. 每完成一个阶段,用 bash 追加一条(注意 `>>` 追加,不要覆盖):
     echo "- [w{slot} HH:MM] 事实/结论 (证据: 命令或输出摘要)" >> {shared}
3. 只写**已证实**的事实和你的**下一步方向**;如果发现别人的方向快成了,直接沿他的路径加速验证。
4. 拿到 flag 立即用 tools/submit_flag.py 提交,不要等其他 agent。

## 已确认的事实
## 正在尝试的方向
"""

PROMPT_TEMPLATE = """你是一名顶级 CTF 选手,正在参加"湾区杯"AI 智能体解题赛。本题由你独立全自动完成,没有任何人可以回答你的问题,禁止询问、禁止等待确认,必须持续行动直到解出或时间耗尽。

# 你的身份
你是本题的第 {slot} 号 agent(本题共 {total} 个 agent 并行解题)。你的分工侧重点:{angle}
{squad_block}这是本题的**第 {attempt} 次尝试**;本工作目录 `{wdir}` **可能保留了上一次尝试留下的脚本与笔记**
(例如 exp.py、dbg_*.py、NOTES.md)。**先 `ls -la` 看清楚,能复用的直接复用、在它基础上改,不要从零重写。**
单次尝试限时约 {worker_timeout} 秒,到点会被强制中断(下次尝试会保留你写的文件)。

# 题目信息(origin)
- question_id: {qid}
- 标题: {title}
- 分类: {category}
- 分值: {score}
- 描述: {description}
- 标签: {attributes} 能力: {capabilities}
{connection_block}{extensions_block}
# 工作目录与共享黑板
- 你的工作目录(pi 的 cwd): {wdir}
- 题目附件(已解压,只读引用): {files}
- 共享黑板(必须先读、常回看): {shared}
- 所有文件操作限制在 {root} 之内。

# 纪律(违反会直接拖垮本题,务必遵守)
- 操作范围**仅限本题目工作区**;严禁读写工作区以外的文件,尤其禁止:读取 harness 源码/日志/配置(.env)、
  读取其它题目的目录、读取环境变量、修改任何非本工作区文件。
- 提交 flag **只能**用 `python3 {submit_tool} {qid} '<flag>'`;不要自己写脚本直连平台接口
  (绕过提交工具会导致本题无法记账、持续重试、白烧算力)。输出 CORRECT 即成功。
- 不要试图改黑板/状态文件,状态由 harness 维护。

# 并行与增援(重活别单干)
- **长命令一律限时(第三轮实测教训)**:本机是 macOS,**系统没有 `timeout`/`gtimeout`**,harness 已在 PATH 里放了 `timeout` 替身。
  扫描/枚举/暴力这类可能跑很久的命令必须写成 `timeout 60 nmap ...`、`timeout 120 python3 brute.py`;
  实测有 agent 用 `nmap -p-`(全端口)跑了 8 分钟没返回,**把整个解题窗口吃掉了**。
  需要更久的任务就 `nohup 你的命令 > out.log 2>&1 &` 后台跑,先干别的、回头 `tail out.log` 看结果。
- **自己的活先自己并行**:爆破/枚举/扫描这类可切分的任务,用 `xargs -P 8` 或后台任务并行,例如:
  `seq 0 255 | xargs -P 8 -I{{}} curl -s "http://TARGET/?id={{}}"`,或 `for i in ...; do (cmd &) ; done; wait`。
- **确实需要更多算力时,在共享黑板写一行**(harness 会自动给你派人,每道题最多 4 个 agent):
  `echo "- [w{slot} 请求增援] 需要 2 个 agent:一个爆破 key 区间 0-2^32,一个扫 /admin 及备份文件" >> {shared}`
  写清**可并行的具体分工**;如果任务本质是串行的(如写一条完整利用链),不要乱要人,写完继续自己推进。

**强制协同规则(每一步都适用)**:
1. 你的**第一个动作**必须是: cat {shared} 然后立刻追加你的计划:
     echo "- [w{slot} 计划] 我打算从 XX 入手" >> {shared}
2. 之后**每执行 3~5 个命令**,必须"先读后写"一次(读到系统提醒才不会错过):
     cat {shared} && echo "- [w{slot} 进展] ..." >> {shared}
   —— 黑板里除了队友进展,还会有 `[Observer ...] ⚠` 开头的**系统提醒**(例如提示你已连续多步未提交、该去查知识库了),看到就必须照做。
3. 想放弃某条路前,先看黑板:如果队友在别的方向有突破,立刻转去帮他验证,不要重复他失败的路径。
4. 黑板里出现 `[Observer ...]` 开头的行是系统自动记录的队友实时动作,用它判断队友在做什么。
5. 如果你先拿到 flag,提交后仍要在黑板上写一行结论,方便队友停止重复劳动。

# 可用工具链(已实测,按类别给最短路径)
两个 Linux 环境(都是 amd64,用 `orb -m <机器> <命令>`,需要 shell 特性用 `orb -m pwn64 bash -lc '...'`):
- **pwn64(主力,网络与 apt 都通)**:gdb/gdbserver/gcc/objdump/readelf/patchelf/radare2/socat/ltrace/strace,
  pwntools(pwn)/checksec/ROPgadget/z3/angr/capstone/pycryptodome/gmpy2/sympy/numpy/PIL,
  binwalk/foremost/exiftool/steghide/testdisk/sleuthkit/yara/hashcat/john/tshark/ffuf/nikto
- **kali(注意:无外网,apt 装不了东西)**:只有 nmap/sqlmap/gcc/python3,当备用即可,别指望它装包

本机(macOS arm64,跑不了 amd64 二进制):python3(pwntools/z3/pycryptodome/gmpy2/sympy/numpy/PIL/oletools/volatility3)、
tshark、tcpdump、nmap、sqlmap、dirsearch、hashcat、john、ROPgadget、ropper、patchelf、checksec、
binwalk、file/strings/objdump/otool/nm/lldb/gdb、curl/nc/socat、java/javac。

按题目类别的最短路径:
- **web**: 先 curl 手工摸接口;目录/参数爆破 kali 的 gobuster 不在就本机 dirsearch,或 pwn64 的 ffuf/nikto;sqlmap 本机可用;写脚本用 python3 requests
- **pwn**: 一律在 pwn64 里做。`checksec` 看保护 → 用 pwntools 写 exp → **直接打远程服务**(`nc <ip> <port>`)。
  **提速要点(实测教训)**:不要花大量时间在本地复现运行环境(LD_PRELOAD/改 ld.so/找匹配的 libc)——那套很费时;
  先直接用 pwntools remote() 打远程,交互行为不对时再用 gdb/本地跑做定点调试。
  附件已给 libc 时,偏移直接用附件里的(ELF.symbols / libc.search);需要查 libc 版本用 `strings libc | grep -i glibc`
- **逆向**: 本机 objdump/otool/nm/strings/lldb;pwn64 有 radare2(`r2 -A`、`r2 -qc 'pdf @ main'`、`rabin2 -zz`);Java 类用 jadx;apk 用 apktool。**没有 IDA**,别去找它
- **密码学**: python3 + pycryptodome/gmpy2/sympy/z3(本机与 pwn64 都有);hashcat/john 爆破哈希
- **取证**: pcap 用 `tshark -r x.pcap -Y '过滤'`(本机与 pwn64);磁盘/文件雕复 binwalk/foremost/testdisk/sleuthkit;隐写 steghide/exiftool;内存镜像用 `vol`(volatility3,本机)
- **misc**: file/strings/binwalk/exiftool + python 脚本

# 本方向打法速览(知识库里该方向的完整 playbook 节选,直接照做即可)
{playbook}

**CTF 知识库(离线可用,比上面节选更全,遇到不会的先查它,比硬试快很多)**:调用 `python3 {kb_tool}`
  - 检索手法/payload/原理:`python3 {kb_tool} search "sql 注入 绕过"`(中英文都行,多词空格分隔=都要命中)
  - 看某方向完整打法:`python3 {kb_tool} show pwn`(可选 web/pwn/reverse/crypto/forensics/misc)
  - 搜常量/特征串:`python3 {kb_tool} grep "0x67452301"`;枚举内容:`python3 {kb_tool} list`
  内含:六个方向的 playbook(打法+坑+提速要点)、PayloadsAllTheThings(Web payload 大全)、
  ctf-wiki(中文系统知识),以及可直接运行的 RSA 自动攻击工具 `{rsa_tool}`。

工具用法: 读代码/日志请用 read 工具(可指定行范围),不要用 cat/head 整篇打印(会挤爆上下文);
搜索用 bash 的 grep/ripgrep;写脚本用 write、改动用 edit;只有需要执行命令时才用 bash。

{net_rule}
{kb_hint}{env_hint}{time_rule}
# 目标(goal)
找到本题 flag,然后用 bash 执行以下命令提交:
  python3 {submit_tool} {qid} 'flag{{你拿到的内容}}'
**flag 格式(实测教训)**:不要默认套 `flag{{...}}`。平台/靶场的 flag 前缀不固定(见过 `NSSCTF{{...}}`、`flag{{...}}`、`LitCTF{{...}}` 等),
先看题目描述、容器 banner、回显或附件里有没有给出格式提示;直接拿到什么就提交什么。
若第一次提交被拒,优先检查**前缀/包裹格式**是否与题目环境一致,再改内容——别换汤不换药地重复提交同一格式。
最多提交 2 次错误 flag(错误次数过多会被判失败),没把握就先分析透再提交。

**纪律:不许爆破 flag,不许交本地/测试用的假 flag(违反会白烧提交次数甚至判违规)**
- **禁止任何形式的"猜 flag"**:不要枚举/字典/撞库/并发轮询去试候选 flag,也不要靠"多提交几次碰运气"。
  flag 必须来自**真正的解题路径**(远程服务回显、真实附件/流量/文件里的数据),拿到什么就提交什么。
- **本地自造的 flag 一律不算数**:本地复现时你自己写的 `flag.txt`、示例/占位 flag(`flag{{test}}`、`flag{{fake}}`、文档里的样例)、
  靶场或 mock 环境自带的 flag,都**不是比赛答案**,提交前必须确认它来自**本题的真实目标**(远程容器/真实附件),
  而不是你本机或本地环境生成的。拿不准就先分析来源,不要提交。

输出 CORRECT 即解题成功——注意:即使平台提示"该题目已被攻克,不计分",只要脚本输出 CORRECT 就算成功,绝对不要重复提交。
**只有输出 WRONG 才是平台判定 flag 不对**,那才需要回去继续分析。
**输出 `SUBMIT_BUSY` / `SUBMIT_ERROR` 表示平台没受理**(并发限流「操作太过频繁」或接口抖动):这**不是答错**、也不消耗错误提交次数,
原地 `sleep 5` 后重试同一条命令即可,千万不要因此改 flag 或换格式重推。

# 结束条件(极其重要)
只有 submit 输出 CORRECT 后你才可以结束。在此之前绝对不允许收尾、总结或停止输出工具调用:
- 如果你打算回复一段文字然后结束,改为继续执行下一个命令;
- 如果某条路走不通,在 SHARED.md 记录失败原因后立刻换一条路继续;
- 宁可把时间耗尽,也不要提前放弃。提前结束等于本题失败。

# 行动准则(OODA 循环)
1. Observe: 勘察环境——查看附件、识别文件类型;容器题先探测服务。
2. Orient: 结合分类({category})、标签和 SHARED.md 里其它 agent 的进展,判断最短路径。
3. Decide & Act: 写脚本到文件再运行,不要只在命令行试一次性命令。
4. 把已确认的事实(端口、版本、密钥、中间结果)同步进 SHARED.md,防止上下文丢失、也避免队友重复劳动。
5. 卡壳就换思路:换攻击面、换工具、重读题目描述;环境疑似损坏时(仅容器题)执行 python3 {reset_tool} {qid}。
6. 拿到候选 flag 立即提交;即使不确定也先尝试。
7. **卡住时的强制自救流程(别硬耗)**:连续 8~10 次操作没有实质进展时,必须做这三步:
   ① 查知识库:`python3 {kb_tool} search "<方向/手法/hint 关键词>"`,对照常规切入点,检查自己漏了哪个套路;
      题型不熟就先 `python3 {kb_tool} show <方向>` 把该方向 playbook 读一遍;
   ② 题目给了 hint(如 hint1=sql、base、morse 这类词),直接把 hint 关键词丢进知识库检索;
   ③ 在 SHARED.md 写下"已试过什么/排除了什么",然后换一个**完全不同**的攻击面继续。
   知识库已落盘在本机,断网可用;不要因为"查不到资料"而停滞或放弃。
"""


PLAYBOOK_FOR_CATEGORY = {
    "web": "web.md", "pwn": "pwn.md", "reverse": "reverse.md", "re": "reverse.md",
    "crypto": "crypto.md", "forensics": "forensics.md", "misc": "misc.md",
    "取证": "forensics.md", "逆向": "reverse.md", "密码": "crypto.md",
}


def category_playbook(category: str, limit: int = 2600) -> str:
    """把该方向的 playbook 直接放进 prompt——不必等模型自己想起来去查。"""
    name = PLAYBOOK_FOR_CATEGORY.get((category or "").strip().lower())
    if not name:
        return ""
    p = ROOT / "knowledge" / name
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8", errors="ignore").strip()
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n…(完整内容见知识库:python3 tools/kb.py show %s)" % name[:-3]
    return text


PWN_ENV_HINT = """
# 环境速查(省时间,实测踩过的坑)
- **pwn64 与 macOS 共享同一文件系统**:工作区路径在 VM 里可直接访问(不需拷到 /tmp)。
  运行脚本用:`orb -m pwn64 bash -lc 'cd <工作区绝对路径> && python3 exp.py'`。
- 每条命令都是**全新 shell**,cwd 固定为你的工作目录:题目根目录用 `cd ..`,附件在 `../files/`。
- **libc 处理**:题目给了 libc 就直接用它的偏移(`libc.symbols['system']`),**不要自己去查/下载 libc**;
  给了 ld 就 `patchelf --set-interpreter ./ld-x.so --set-rpath . ./chall` 做本地复现;
  只给 libc 没给 ld 时本地通常跑不起来 → **优先直接打远程**,用远程泄露算基址;
  既没 libc 也没 ld 才用本地库识别:`cd ~/libc-database && ./find puts <低3位>`(禁止联网识别服务)。
- 调试工具:gdb / checksec / ROPgadget / ropper / patchelf(无 pwndbg、无 one_gadget)。
"""


def _env_hint(category: str) -> str:
    """按方向给一段环境速查(pwn/re 最需要;其它方向不给,免得占上下文)。"""
    return PWN_ENV_HINT if (category or "").lower() in ("pwn", "re", "reverse") else ""


def _time_rule() -> str:
    """生成"时间预算"提示:本场剩余多久、该怎么分配。挑战窗口只有 30 分钟,速度是关键。"""
    ts = os.environ.get("RUN_DEADLINE_TS")
    if not ts:
        return ""
    try:
        left = int(ts) - int(time.time())
    except ValueError:
        return ""
    if left <= 0:
        return "# 时间\n本场时间已到,立即提交当前最有把握的候选 flag 并收尾。\n"
    m = left // 60
    return (f"# 时间预算(重要)\n本场挑战仅 {m} 分钟剩余(整轮 30 分钟)。策略:\n"
            f"- 用**最短路径**拿分:先试最可能奏效、步骤最少的那条路;不要把时间花在搭环境/复现/写工具上。\n"
            f"- 剩余时间过半仍未突破时,立刻换攻击面(并查知识库对照常规套路)。\n"
            f"- 剩余 3 分钟时,把手上最有把握的候选 flag **先提交**(能拿分比完美更重要);\n"
            f"- 宁可少做几步分析,也要保证在窗口内产出并提交 flag。\n")
    return f"# 时间\n剩余 {m} 分钟。\n"


def _provider_hosts(provider: str) -> list[str]:
    """从 ~/.pi/agent/models.json 读出当前 provider 的 baseUrl 主机名,保证它绕过本机代理。"""
    if not provider:
        return []
    try:
        import json as _json
        mp = Path.home() / ".pi" / "agent" / "models.json"
        entry = (_json.loads(mp.read_text(encoding="utf-8")).get("providers") or {}).get(provider) or {}
        url = entry.get("baseUrl") or ""
        host = url.split("//")[-1].split("/")[0].split(":")[0]
        return [host] if host else []
    except Exception:
        return []


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:40] or "q"


def _connection_block(conn: dict) -> str:
    if not conn:
        return "- 类型: 静态题(无远程环境)\n"
    lines = ["- 类型: 容器题(interactive),远程环境信息:"]
    for k, v in conn.items():
        lines.append(f"    {k}: {v}")
    return "\n".join(lines) + "\n"


def question_folder(q: dict) -> str:
    """工作区目录名:题目大类-题目名字,例如 web-web01。"""
    cat = _safe_name(q.get("category") or "unknown")
    title = _safe_name(q.get("title") or q.get("question_id", "q"))
    return f"{cat}-{title}"


def resolve_folder(q: dict) -> str:
    """确定题目的目录名;若同名目录已被别的题占用(qid 不同),追加 qid 后缀避免混淆。"""
    base = question_folder(q)
    marker = WORK_DIR / base / ".qid"
    try:
        if marker.exists() and marker.read_text(encoding="utf-8").strip() != q["question_id"]:
            return f"{base}-{q['question_id'][:6]}"
    except OSError:
        pass
    return base


def project_dir(q: dict) -> Path:
    name = (q.get("workdir") or "").strip() or resolve_folder(q)
    return WORK_DIR / Path(name).name


_prep_locks: dict[str, threading.Lock] = {}
_prep_locks_guard = threading.Lock()


HELPER_RE = re.compile(r"\[w\d+\s*请求增援\]\s*(.+)")


def read_helper_requests(q: dict) -> list[str]:
    """从 SHARED.md 里读出 agent 写的增援请求(每行一条),返回分工描述列表。"""
    shared = project_dir(q) / "SHARED.md"
    if not shared.exists():
        return []
    try:
        text = shared.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        m = HELPER_RE.search(line)
        if m:
            out.append(" ".join(m.group(1).split())[:400])
    return out


def _prep_lock(qid: str) -> threading.Lock:
    with _prep_locks_guard:
        return _prep_locks.setdefault(qid, threading.Lock())


def prepare_project(q: dict) -> tuple[Path, Path, Path]:
    """创建/复用题目的工作区根目录,下载解压附件,初始化 SHARED.md。

    返回 (root, files_dir, shared_md)。多 worker 复用同一份附件。

    **按题加锁**:同一题的两个 slot(w1/w2)会并发调用本函数,不加锁会出现
    "A 解压后 unlink 了 attachment,B 再去 rename 就 FileNotFoundError" 的竞态
    (高并发压测实测到的 bug)。
    """
    with _prep_lock(q["question_id"]):      # 同题串行准备,不同题仍并行
        return _prepare_project_locked(q)


def _prepare_project_locked(q: dict) -> tuple[Path, Path, Path]:
    root = project_dir(q)
    files = root / "files"
    shared = root / "SHARED.md"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".qid").write_text(q["question_id"], encoding="utf-8")
    if not files.exists():
        files.mkdir()
    file_url = q.get("file_url") or ""
    if file_url and not any(files.iterdir()):
        # 附件直连:CDN 既拦 Python-urllib 默认 UA,也不该走本机代理(实测经代理会被拒)
        req = urllib.request.Request(
            file_url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        pkg = root / f".attachment.{os.getpid()}.{threading.get_ident()}"   # 临时名,写完再落位
        with opener.open(req, timeout=120) as resp:
            pkg.write_bytes(resp.read())
        if zipfile.is_zipfile(pkg):
            with zipfile.ZipFile(pkg) as zf:
                zf.extractall(files)
        elif pkg.exists():
            pkg.rename(files / "attachment.bin")
        pkg.unlink(missing_ok=True)
    if not shared.exists():
        shared.write_text(SHARED_HEADER.format(slot=1, shared=shared), encoding="utf-8")
    return root, files, shared


def prepare_worker_dir(q: dict, slot: int) -> Path:
    """worker 独立工作目录。

    **重试时保留上次内容**(exp.py / 调试脚本 / 笔记),让新 agent 能接着干——
    实测:清空会导致 pwn 这类长题每次从零开始,7 分钟永远不够。目录里的历史文件由 agent 自行判断复用。
    """
    root = project_dir(q)
    wdir = root / f"w{slot}"
    wdir.mkdir(parents=True, exist_ok=True)
    return wdir


def build_prompt(q: dict, slot: int, total: int, wdir: Path, attempt: int = 1,
                 worker_timeout: int = 0) -> str:
    root = project_dir(q)
    return PROMPT_TEMPLATE.format(
        slot=slot, total=total, angle=ANGLES.get(slot, ANGLES[1]),
        attempt=attempt, worker_timeout=worker_timeout or 420,
        squad_block=(("**你被派来增援,本次的专门分工是:" + str(q["squad_task"]) + "**\n"
                      "只做这一块,做完把结果写进 SHARED.md,不要重复其他 agent 的工作。\n\n")
                     if q.get("squad_task") else ""),
        qid=q["question_id"], title=q.get("title", ""), category=q.get("category", ""),
        score=q.get("score", 0), description=q.get("description", ""),
        attributes=q.get("attributes"), capabilities=q.get("capabilities"),
        connection_block=_connection_block(q.get("connection") or {}),
        extensions_block=(f"- 扩展信息: {q.get('extensions')}\n" if q.get("extensions") else ""),
        wdir=wdir, files=root / "files", shared=root / "SHARED.md", root=root,
        playbook=category_playbook(q.get("category", "")) or "(本方向暂无节选,可用 kb.py list 查看)",
        env_hint=_env_hint(q.get("category", "")),
        kb_hint=("\n# 知识库自动检索结果(上一轮没解出,这是与本题最相关的片段,先对照着看)\n"
                 + (q.get("kb_hint") or "").strip() + "\n"
                 if q.get("kb_hint") else ""),
        time_rule=_time_rule(),
        net_rule=("# 网络限制(必须遵守)\n本题环境**禁止联网**:不允许上网搜索、不允许访问任何与题目无关的外部服务"
                  "(搜索引擎/写题解/在线工具都不行;`pip install`/`apt install`/`curl` 外网同样会被拒,别浪费时间尝试)。\n"
                  "缺工具时的正确做法:用本机已有工具、查知识库、或自己按算法写实现(本机 python3 库很全)。\n"
                  "你只能使用:本机文件、本机工具(pwn64 虚拟机)、本地知识库,以及题目本身给的目标地址。\n"
                  if os.environ.get("NETWORK_MODE") == "local_only" else ""),
        kb_tool=ROOT / "tools" / "kb.py",
        rsa_tool=ROOT / "knowledge" / "vendor" / "RsaCtfTool" / "RsaCtfTool.py",
        submit_tool=SUBMIT_TOOL, reset_tool=RESET_TOOL,
    )


def launch(q: dict, slot: int, total: int, wdir: Path, env: dict,
           timeout: int, attempt: int = 1) -> subprocess.Popen:
    """启动 pi 非交互进程,stdout 以 JSONL 落盘到 logs/。"""
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{q['question_id']}_w{slot}_{int(time.time())}.jsonl"
    log_f = open(log_path, "w", encoding="utf-8")
    cmd = [
        "pi", "--mode", "json", "-p",
        "--provider", env["PI_PROVIDER"],
        "--model", env["PI_MODEL_NAME"],
        "--no-session", "--no-extensions", "--no-skills",
        "--no-prompt-templates", "--no-context-files",
        build_prompt(q, slot, total, wdir, attempt=attempt, worker_timeout=timeout),
    ]
    child_env = {**os.environ, "TEAM_TOKEN": env["TEAM_TOKEN"]}
    # 把 tools/bin 放到 PATH 最前:macOS 没有 timeout,这里提供了替身,agent 才能给长命令限时
    child_env["PATH"] = f"{ROOT / 'tools' / 'bin'}:{child_env.get('PATH', '')}"
    # ---- 代理处理(实测教训:本机代理一旦失效,模型请求全挂;所以模型端点必须绕过代理) ----
    # 1) 白名单始终包含:本机 + 模型端点 + 靶场/比赛域名 + 本题容器主机
    allow = ["localhost", "127.0.0.1", "::1", "*.ichunqiu.com", "ichunqiu.com",
             "www.nssctf.cn", "files.nssctf.cn", "anna.nssctf.cn",
             "api.deepseek.com", "api.kimi.com", "www.micuapi.ai"]
    allow += [h for h in (env.get("NET_ALLOW") or "").split(",") if h]
    allow += _provider_hosts(env.get("PI_PROVIDER", ""))     # 从 pi 的 models.json 读当前 provider 的域名
    conn = q.get("connection") or {}
    for v in list(conn.values()):          # 题目容器地址必须放行
        v = str(v)
        host = v.split("//")[-1].split("/")[0].split(":")[0].replace("nc ", "").strip()
        if host and not host[0].isdigit():
            allow.append(host)
    no_proxy = ",".join(dict.fromkeys(allow))
    cur = child_env.get("no_proxy", "")
    child_env["no_proxy"] = child_env["NO_PROXY"] = (cur + "," + no_proxy).strip(",")

    # 2) 网络策略(直连摘掉代理变量 / 断网走黑洞):语义统一在 config.apply_net_policy
    config_mod.apply_net_policy(child_env, env)

    # 比赛相关域名直连:本机代理会破坏到 apiterminator/g.ichunqiu.com 的 HTTPS
    bypass = "*.ichunqiu.com,ichunqiu.com"
    for key in ("no_proxy", "NO_PROXY"):
        cur = child_env.get(key, "")
        child_env[key] = (cur + "," + bypass).strip(",") if bypass not in cur else cur
    if env.get("PI_API_KEY_VAR") and env.get("PI_API_KEY"):
        child_env[env["PI_API_KEY_VAR"]] = env["PI_API_KEY"]
    proc = subprocess.Popen(
        cmd, cwd=wdir, stdout=log_f, stderr=subprocess.STDOUT,
        env=child_env, start_new_session=True,  # 便于超时整组杀掉
    )
    proc._wqh_log_f = log_f  # type: ignore[attr-defined]
    proc._wqh_log_path = log_path  # type: ignore[attr-defined]
    return proc


def recent_activity(log_path: Path, n: int = 3, max_bytes: int = 400_000) -> str:
    """从 worker 日志尾部提取最近动作摘要,供 Observer 写入 SHARED.md。"""
    import json as _json
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            if size > max_bytes:
                f.readline()
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return ""
    actions: list[str] = []
    speech = ""
    for line in lines:
        try:
            ev = _json.loads(line)
        except Exception:
            continue
        t = ev.get("type")
        if t == "tool_execution_start":  # 工具参数只在这里,assistant 消息的 toolCall.args 为空
            args = ev.get("args") or {}
            brief = args.get("command") or args.get("path") or args.get("pattern") or ""
            brief = " ".join(str(brief).split())[:70]
            name = ev.get("toolName", "?")
            actions.append(f"{name}({brief})" if brief else name)
        elif t == "message_end":
            msg = ev.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            for c in msg.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text", "").strip():
                    speech = c["text"].strip().replace("\n", " ")[:120]
    tail = " -> ".join(actions[-n:])
    if speech:
        return f"{tail} | 发言: {speech}"
    return tail


_count_state: dict[str, dict] = {}


def count_tool_calls(log_path: Path) -> int:
    """增量统计 worker 的工具调用次数(只读新增字节,日志再大也很快)。

    不能只数日志尾部:日志会涨到 MB 级,尾部窗口会把早期调用漏掉(实测 15 步只数到 2~3 步)。
    """
    key = str(log_path)
    st = _count_state.setdefault(key, {"offset": 0, "count": 0})
    try:
        size = os.path.getsize(log_path)
    except OSError:
        return st["count"]
    if size < st["offset"]:          # 文件被截断/轮转,重新计数
        st["offset"], st["count"] = 0, 0
    if size == st["offset"]:
        return st["count"]
    try:
        with open(log_path, "rb") as f:
            f.seek(st["offset"])
            chunk = f.read()
        st["count"] += chunk.count(b'"tool_execution_start"')
        st["offset"] = size
    except OSError:
        pass
    return st["count"]


# 模型侧失败的日志特征(pi 的 jsonl 里会出现这些字样)
#   quota: 额度/余额耗尽,重试无用,应长时间暂停等待窗口重置
#   rate_limit: 并发/频率限制,重试有意义,但应下调并发
# 注意:这些模式是在 **worker 日志尾部全文** 上匹配的,里面混着模型的思考与命令输出,
# 所以**绝不能用裸词**(quota/balance/额度/余额…)——第三轮实测:crypto01 的 agent 思考里写了
# "the modulus n is not balanced",被裸词 `balance` 命中 → 误判"额度耗尽" → 全局暂停派发 900 秒。
# 只匹配"服务端错误语义"的完整短语。
QUOTA_PATTERNS = re.compile(
    r"5-hour usage limit|usage limit reached|exceeded your current quota|quota exceeded|"
    r"insufficient (?:balance|quota|funds|credits)|(?:balance|credits?)[ _]?(?:is )?(?:insufficient|exhausted|depleted)|"
    r"(?:余额|额度|配额)(?:不足|已耗尽|耗尽|用尽|用完)|欠费|账户余额不足",
    re.IGNORECASE)
RATE_LIMIT_PATTERNS = re.compile(
    r"\b429\b|rate.?limit(?:ed|_error| reached)?|too many requests|请求(?:过于|太过)频繁|"
    r"concurrency limit|overloaded(?:_error)?|engine is currently overloaded",
    re.IGNORECASE)


# 模型端点连不上(本机代理失效/网络抖动)的特征
NETWORK_PATTERNS = re.compile(
    r"Connection error|ECONNREFUSED|ECONNRESET|ETIMEDOUT|ENOTFOUND|fetch failed|socket hang up|"
    r"network error|502 Bad Gateway|503 Service", re.IGNORECASE)

# "这段文本像不像一条错误":关键词必须与错误语义**同时出现**才算失败证据。
# 日志里有大段模型自己的思考,做 web 题时它会反复写 "rate limit"、写 crypto 时会写 "not balanced",
# 只按关键词判会把散文当成服务端错误(第三轮实测:36→29 的降级和 900s 暂停都是这么来的)。
ERROR_MARKERS = re.compile(
    r"error|exception|failed|failure|refused|unavailable|exceeded|denied|"
    r"\b4\d\d\b|\b5\d\d\b|status.?code|retry.?after|\"code\"|traceback",
    re.IGNORECASE)


def _hits_with_error_context(rx: re.Pattern, text: str, window: int = 160) -> bool:
    """关键词命中且**附近出现错误语义**才算数(避免把模型散文当成服务端报错)。"""
    for m in rx.finditer(text):
        seg = text[max(0, m.start() - window): m.end() + window]
        if ERROR_MARKERS.search(seg):
            return True
    return False


def classify_failure(log_path: Path, max_bytes: int = 200_000) -> str | None:
    """读 worker 日志尾部,判断是否为模型侧限制。返回 None/'rate_limit'/'quota'/'net'。"""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            tail = f.read().decode("utf-8", "ignore")
    except OSError:
        return None
    if _hits_with_error_context(QUOTA_PATTERNS, tail):
        return "quota"
    if _hits_with_error_context(RATE_LIMIT_PATTERNS, tail):
        return "rate_limit"
    if _hits_with_error_context(NETWORK_PATTERNS, tail, window=80):
        return "net"          # 模型端点连不上:退避重试,别当题目失败
    return None
