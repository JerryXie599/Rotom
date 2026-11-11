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
import tarfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import threading

import config as config_mod
import profile as profile_mod
import textutil

ROOT = Path(__file__).resolve().parent          # 代码位置(工具脚本在 ROOT/tools 下)
RUN_DIR = Path(os.environ.get("ROTOM_RUN_DIR") or ROOT)  # 本项目的数据目录
WORK_DIR = RUN_DIR / "work"
LOG_DIR = RUN_DIR / "logs"
SUBMIT_TOOL = ROOT / "tools" / "submit_flag.py"
RESET_TOOL = ROOT / "tools" / "reset_env.py"
REPORT_TOOL = ROOT / "tools" / "report_finding.py"      # 正式渗透:记录 finding(finding 模式用)

PROF = profile_mod.current()   # 当前策略包:决定提示词包 / playbook 映射 / 环境速查 / 连接块风格

# 多 agent 同题时的分工,按 slot 区分
ANGLES = {
    1: "主攻最短、最可能的利用路径(先拿最容易的突破点)",
    2: "走与其它 agent 不同的攻击面:换端点/换漏洞类型/换工具链,严禁重复别人已试过且失败的思路",
    3: "交叉验证与补漏:复核其它 agent 的结论与中间产物,重点排查他们忽略的细节和已知写法的变种",
}

# ---- 提示词包(策略包决定用哪一份,见 prompts/<pack>/prompt.md)----
# 模板里的 @@名字@@ 是占位符,由 build_prompt 统一替换;用 @@@@ 而不是 {} 是为了让
# 提示词文件里的 JSON / 代码花括号可以随便写、不用转义(改文案不再需要动 Python)。
PROMPTS_DIR = ROOT / "prompts"

# 没有 profiles/<name>.json playbook 映射时的兜底(与历史行为一致)
PLAYBOOK_FOR_CATEGORY_FALLBACK = {
    "pentest": "pentest.md", "web": "web.md", "pwn": "pwn.md", "reverse": "reverse.md", "re": "reverse.md",
    "crypto": "crypto.md", "forensics": "forensics.md", "misc": "misc.md",
    "取证": "forensics.md", "逆向": "reverse.md", "密码": "crypto.md",
}


_PLACEHOLDER = re.compile(r"@@([A-Z_]+)@@")


def _expand(text: str, mapping: dict) -> str:
    """替换 @@名字@@ 占位符;不在映射表里的原样保留(便于发现模板写错的名字)。"""
    def sub(m: "re.Match[str]") -> str:
        key = m.group(1)
        return str(mapping[key]) if key in mapping else m.group(0)
    return _PLACEHOLDER.sub(sub, text)


def _pack_dir() -> Path:
    """当前策略包的提示词目录;缺 prompt.md 时退回 ctf 包(保证永远能组装出 prompt)。"""
    d = PROMPTS_DIR / PROF.prompt_pack
    return d if (d / "prompt.md").exists() else PROMPTS_DIR / "ctf"


def prompt_template() -> str:
    return (_pack_dir() / "prompt.md").read_text(encoding="utf-8", errors="ignore")


def shared_header(slot: int, shared: Path) -> str:
    """共享黑板(SHARED.md)的表头,同样按策略包取。"""
    text = (_pack_dir() / "shared.md").read_text(encoding="utf-8", errors="ignore")
    return _expand(text, {"SLOT": slot, "SHARED": shared})


def system_prompt_file() -> Path | None:
    """pi 的系统提示词文件(策略包专属,如 prompts/pentest/system.md);缺文件返回 None。

    这是**系统层**的行为准则(身份/红线/自主性),与任务消息分开:
    pi 用 --append-system-prompt 把它追加到内置系统提示词后面,遵守度比写在任务消息里更硬。
    """
    p = PROMPTS_DIR / PROF.system_prompt
    return p if p.exists() else None


def category_playbook(category: str, limit: int = 2600) -> str:
    """把该方向的 playbook 直接放进 prompt——不必等模型自己想起来去查。"""
    mapping = PROF.playbooks() or PLAYBOOK_FOR_CATEGORY_FALLBACK
    name = mapping.get((category or "").strip().lower())
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


PENTEST_ENV_HINT = """
# 环境速查(省时间)
- 每条 bash 命令都是**全新 shell**,cwd 固定为你的工作目录;题目根目录用 `cd ..`。
- 目标地址在「目标信息」里(形如 `10.x.x.x:8080`)。**先 curl 探 http/https 再动手**,不要一上来就上工具。
- 要跑 Linux 专属工具(ffuf/nikto/gcc 等):`orb -m pwn64 bash -lc 'cd <工作区绝对路径> && ...'`(与 macOS 共享文件系统)。
- 本机 nmap **无 root**:必须加 `-sT -Pn`;`-p-` 全端口很慢,先 `-p 1-10000 --min-rate 2000`。
- 写脚本用本机 python3 + requests(已装)。**不要 `pip install`/`apt install`**(现场可能没外网,装了也白等)。
"""


def in_container() -> bool:
    """是不是跑在托管容器里(平台 Docker)。用来把"macOS + pwn64 虚拟机"那套提示换成容器版。"""
    try:
        if os.environ.get("ROTOM_HOSTED") == "1":
            return True
        return Path("/.dockerenv").exists()
    except Exception:
        return False


def platform_tools_note() -> str:
    """工具链的平台差异提示(@@PLATFORM_TOOLS@@)。macOS 上空串(基线不变),容器里点明"工具都是本地的"。"""
    if not in_container():
        return ""
    return ("- **容器内工具链**:上面提到的工具(nmap/gobuster/ffuf/nikto/gcc/strace/radare2/tshark…)"
            "**全部本地可用**,nmap 有 root 权限;`orb -m ...` 只是兼容层写法,等价于本地执行。\n")


def platform_note() -> str:
    """平台差异提示(占位符 @@PLATFORM_NOTE@@)。

    macOS 上返回**空串** —— 保证提示词基线与历史逐字一致;容器里补一句"你就在 Linux 容器里",
    免得 agent 按 macOS 那套绕远(Docker 里 amd64 原生可跑、timeout 是真的、orb 是兼容层、无外网)。
    """
    if not in_container():
        return ""
    return ("(本环境是 Linux 容器:amd64 二进制**原生可执行**,`timeout` 是真的,"
            "`orb ...` 只是历史兼容写法、等价于本地执行;无外网,资料一律查本地知识库)")


PWN_ENV_HINT_HOSTED = """
# 环境速查(省时间,实测踩过的坑)
- **你就在 Linux 容器里(amd64,root)**:自己写的脚本/二进制**原生可执行**,不需要任何虚拟机或兼容层。
- 每条命令都是**全新 shell**,cwd 固定为你的工作目录:题目根目录用 `cd ..`,附件在 `../files/`。
- **libc 处理**:题目给了 libc 就直接用它的偏移(`libc.symbols['system']`),**不要自己去查/下载 libc**(无外网);
  给了 ld 就 `patchelf --set-interpreter ./ld-x.so --set-rpath . ./chall` 做本地复现;
  只给 libc 没给 ld 时本地通常跑不起来 → **优先直接打远程**,用远程泄露算基址。
- 工具已装:gdb / checksec / ROPgadget / ropper / patchelf / gcc / python3+pwntools。
"""

PENTEST_ENV_HINT_HOSTED = """
# 环境速查(省时间)
- 每条 bash 命令都是**全新 shell**,cwd 固定为你的工作目录;题目根目录用 `cd ..`。
- 目标地址在「目标信息」里(形如 `10.x.x.x:8080`)。**先 curl 探 http/https 再动手**,不要一上来就上工具。
- 你就在 Linux 容器里(amd64,root):工具**本地原生**可用 —— nmap 有 root(不用加 `-sT -Pn`),
  ffuf / nikto / gcc / strace / sqlmap / dirsearch / nuclei(带模板)都在;
  `orb ...` 只是历史兼容写法,等价于本地执行,写不写都行。
- 写脚本用本机 python3 + requests / pwntools / pycryptodome(已装)。
  **不要 `pip install` / `apt install`(本环境无外网,装了也白等)**:缺什么就查知识库,或用已有库自己实现。
"""


def _env_hint(category: str) -> str:
    """按方向给一段环境速查:pwn/re 一套;其余方向取决于策略包(default_env_hint)。

    ctf 包不给 web/pwn 之外的环境速查(CTF 平台行为);众测/渗透包默认给"web 渗透速查"。
    托管容器里换成 HOSTED 版文案(没有 pwn64 虚拟机、没有"无 root"这些 macOS 前提)。
    """
    cat = (category or "").strip().lower()
    hosted = in_container()
    if cat in ("pwn", "re", "reverse"):
        return PWN_ENV_HINT_HOSTED if hosted else PWN_ENV_HINT
    if PROF.get("default_env_hint", "pentest") != "pentest":
        return ""
    return PENTEST_ENV_HINT_HOSTED if hosted else PENTEST_ENV_HINT


def _time_rule() -> str:
    """生成"时间预算"提示:本场剩余多久、该怎么分配(只影响这一个 worker 的节奏)。"""
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
    return (f"# 时间预算(重要)\n本场还剩约 {m} 分钟。策略:\n"
            f"- 用**最短路径**拿分:先试最可能奏效、步骤最少的那条路;别把时间花在搭环境/写大工具上。\n"
            f"- 剩余时间过半仍未突破时,立刻换攻击面(并查知识库对照常规套路)。\n"
            f"- 快结束时,把手上最有把握的候选 flag **先提交**(能拿分比完美更重要);\n"
            f"- 宁可少做几步分析,也要保证在窗口内产出并提交 flag。\n")


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


def _benchmark_host() -> list[str]:
    """跑分平台域名(走代理的现场必须让它直连,否则 TLS 会被本机代理破坏)。"""
    url = (os.environ.get("BENCHMARK_BASE_URL") or "").strip()
    host = url.split("//")[-1].split("/")[0].split(":")[0] if url else ""
    return [host] if host else []


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:40] or "q"


def _connection_block(conn: dict) -> str:
    """目标地址块。措辞按策略包的 connection_style 分三种:

      ctf        传统 CTF 题(静态题 / 容器题)
      container  众测/靶场:目标由平台下发,容器可能正在轮换
      target     正式渗透:目标来自授权清单,地址由 harness 同步
    """
    style = PROF.get("connection_style", "container")
    if style == "ctf":
        if not conn:
            return "- 类型: 静态题(无远程环境)\n"
        lines = ["- 类型: 容器题(interactive),远程环境信息:"]
        for k, v in conn.items():
            lines.append(f"    {k}: {v}")
        return "\n".join(lines) + "\n"
    if style == "target":
        if not conn:
            return ("- 目标地址: **暂未就绪**(harness 还没同步到地址)。\n"
                    "  先读 SHARED.md 里队友已确认的信息;不要盲扫,也不要去猜地址。\n")
        lines = ["- 目标地址(授权范围内,可能有多个入口):"]
        for k, v in conn.items():
            lines.append(f"    {k}: {v}")
        lines.append("  先 `curl -k -m 8 -i <url>/` 探一下(HTTP 优先,再试 HTTPS),拿到标题/响应头再决定打法。")
        return "\n".join(lines) + "\n"
    # container(众测):一个目标多个 flag,地址可能因为容器轮换而变
    if not conn:
        return ("- 目标地址: **暂未就绪**(容器还没拉起来或正在轮换)。\n"
                "  这一轮先别猜地址;若你看到这条,说明 harness 稍后会把容器重新拉起,"
                "先把 SHARED.md 里队友的结论读一遍,再从已有的目标地址入手。\n")
    lines = ["- 目标地址(**授权范围内的唯一攻击目标**,可能有多个入口):"]
    for k, v in conn.items():
        lines.append(f"    {k}: {v}")
    lines.append("  先 `curl -k -m 8 -i <url>/` 探一下(HTTP 优先,再试 HTTPS),拿到标题/响应头再决定打法。")
    lines.append("  ⚠ 容器重开后地址会变:**以黑板顶部「当前目标地址(harness 维护)」为准**;"
                 "黑板上其它地址可能是旧容器残留,连不上就别再试。")
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
    """题目工作区根目录。

    workdir 为**绝对路径**时直接采用(涉诈场景:worker 直接落在站点档案的 工作区/ 里,
    证据/报告就地归档;targets.json 是操作者自己生成的,受信)。相对路径/缺省沿用老行为:
    WORK_DIR 下按 "大类-题目名" 建目录(Path(name).name 的截取是防平台数据带斜杠穿越)。
    """
    name = (q.get("workdir") or "").strip()
    if name:
        p = Path(name).expanduser()
        if p.is_absolute():
            return p
    return WORK_DIR / Path(name).name if name else WORK_DIR / resolve_folder(q)


_prep_locks: dict[str, threading.Lock] = {}
_prep_locks_guard = threading.Lock()


HELPER_RE = re.compile(r"\[w\d+\s*请求增援\]\s*(.+)")


def read_helper_requests(q: dict) -> list[str]:
    """从 SHARED.md 里读出 agent 写的增援请求(每行一条),返回分工描述列表。"""
    shared = project_dir(q) / "SHARED.md"
    if not shared.exists():
        return []
    text = textutil.read_text_lossless(shared)      # 黑板可能混进 GBK(agent 直接 >> 了 GBK 输出)
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
    atts = [str(u) for u in (q.get("attachments") or []) if str(u).strip()]
    if not atts and file_url:
        atts = [file_url]
    if atts and not any(files.iterdir()):
        # 附件直连:CDN 既拦 Python-urllib 默认 UA,也不该走本机代理(实测经代理会被拒)。
        # 阶段 2/3 的题可能带 1~6 个附件,逐个下载;zip/tar 自动解包,单文件原样落盘。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for idx, url in enumerate(dict.fromkeys(atts), 1):
            pkg = root / f".attachment.{os.getpid()}.{threading.get_ident()}.{idx}"
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
                with opener.open(req, timeout=120) as resp:
                    pkg.write_bytes(resp.read())
                if zipfile.is_zipfile(pkg):
                    with zipfile.ZipFile(pkg) as zf:
                        zf.extractall(files)
                elif tarfile.is_tarfile(pkg):
                    with tarfile.open(pkg) as tf:
                        try:
                            tf.extractall(files, filter="data")
                        except TypeError:                 # Python < 3.12 没有 filter 参数
                            tf.extractall(files)
                elif pkg.exists():
                    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
                    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:80] or f"attachment{idx}.bin"
                    pkg.rename(files / name)
            except Exception:                             # 单个附件失败不拖累其它附件/起题
                pass
            finally:
                pkg.unlink(missing_ok=True)
    if not shared.exists():
        shared.write_text(shared_header(1, shared), encoding="utf-8")
    return root, files, shared


def _write_cookie_jar(q: dict, wdir: Path) -> None:
    """把平台适配器换好的会话 Cookie 落成 Netscape 格式,worker 起手就能 `curl -b cookies.txt`。

    新榜单平台的入口票据是一次性的:首次访问返回 303 + Set-Cookie,票据当场作废。
    agent 若用不带 jar 的 curl 打开票据链接就会丢会话,后续全是 403(runtime authentication required)。
    这里提前替它写好,提示词里只出现干净 URL。
    """
    cookie = str(q.get("http_cookie") or "").strip()
    if not cookie:
        return
    try:
        conn = q.get("connection") or {}
        url = str(conn.get("target1") or conn.get("url1") or "")
        host = urllib.parse.urlsplit(url).hostname or ""
    except Exception:                                   # noqa: BLE001
        host = ""
    if not host:
        return
    lines = ["# Netscape HTTP Cookie File", "# 由 Rotom 开题时生成:入口票据已兑换成会话 Cookie"]
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip()
        if k:
            lines.append("\t".join([host, "FALSE", "/", "FALSE", "0", k, v]))
    try:
        (wdir / "cookies.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def prepare_worker_dir(q: dict, slot: int) -> Path:
    """worker 独立工作目录。

    **重试时保留上次内容**(exp.py / 调试脚本 / 笔记),让新 agent 能接着干——
    实测:清空会导致 pwn 这类长题每次从零开始,7 分钟永远不够。目录里的历史文件由 agent 自行判断复用。
    """
    root = project_dir(q)
    wdir = root / f"w{slot}"
    wdir.mkdir(parents=True, exist_ok=True)
    if not (wdir / "cookies.txt").exists():
        _write_cookie_jar(q, wdir)
    return wdir


def _shared_facts_block(root: Path, max_lines: int = 70, max_chars: int = 3600) -> str:
    """把上一轮尝试在黑板上沉淀的事实**硬钉进提示词**(不再只靠 agent 自己去 cat)。

    对齐 2026-10-10 XBOW 托管实战复盘(冠军 94.88 的做法):跨代事实以结构化清单进 prompt,
    并写明"实例可能已重置,声称失效的东西必须重新验证"—— 防止把旧容器的死地址当成本轮事实。
    """
    try:
        sp = root / "SHARED.md"
        if not sp.exists():
            return ""
        lines = [ln.rstrip() for ln in sp.read_text(encoding="utf-8", errors="ignore").splitlines()
                 if ln.strip()]
        if len(lines) < 4:            # 只有模板头/空黑板,不值得钉
            return ""
        tail = lines[-max_lines:]
        block = "\n".join(tail)
        if len(block) > max_chars:
            block = block[-max_chars:]
        return ("\n# 前次尝试沉淀的事实(黑板尾部摘录,已替你读好;**实例可能已重置**,其中"
                "\"不通/已排除/已证死\"的结论必须本轮重新验证后才可信,别把旧容器的地址当成本轮事实)\n"
                + block + "\n")
    except Exception:
        return ""


def build_prompt(q: dict, slot: int, total: int, wdir: Path, attempt: int = 1,
                 worker_timeout: int = 0) -> str:
    """按当前策略包的模板组装 prompt:模板里的 @@名字@@ 在这里统一替换。

    模板在 prompts/<pack>/prompt.md(纯文本,改文案不用动代码);
    这里只负责"算值"——所有值都来自题目数据与当前策略包。
    """
    root = project_dir(q)
    flag_count = int(q.get("flag_count") or 1)
    if PROF.goal == "findings":        # 正式渗透:进度是 finding,没有 flag
        got = int(q.get("findings_count") or 0)
        progress = (f"已记录 {got} 条 finding" +
                    (";本目标已交付(--done 过了)" if q.get("delivered")
                     else ";还没交付(测完记得用 --done 收尾)"))
    elif flag_count > 1:
        progress = f"已拿到 {int(q.get('correct_flag_count') or 0)}/{flag_count}"
    else:
        progress = "本题 1 个 flag"
    kb_hint = ("\n# 知识库自动检索结果(上一轮没解出,这是与本题最相关的片段,先对照着看)\n"
               + (q.get("kb_hint") or "").strip() + "\n") if q.get("kb_hint") else ""
    auto_hint = (q.get("hint_text") or "").strip()
    if auto_hint:
        hint_block = ("\n# 平台提示(harness 已替你取回,该题得分已按 10% 折减,直接用别浪费)\n"
                      + auto_hint + "\n")
    else:
        # 2026-10-26 用户拍板:提示**不强制注入**,由 agent 自行判断要不要(能自己解就省下 10% 扣分)。
        # harness 只告诉它工具在哪、代价多少、什么时候值得拿 —— 取不取它自己定。
        hint_block = ("\n# 平台提示工具(自取自用,不强制)\n"
                      f"卡住时可以拿本题的平台提示:`python3 {ROOT / 'tools' / 'get_hint.py'} {q['question_id']}`。\n"
                      "代价:该题**只要取过一次**,得分按 10% 折减(easy -20 / medium -30 / hard -50)。所以:能自己解就别拿;"
                      "方向穷尽、或卡了十来分钟没头绪再拿;拿到就立刻照提示走,别重复侦察。\n")
        if attempt >= 2:
            hint_block += (f"**这是第 {attempt} 次尝试,上一轮没解出 —— 强烈建议先拿提示再动手**"
                           f"(`python3 {ROOT / 'tools' / 'get_hint.py'} {q['question_id']}`)。\n")
    shared_facts = _shared_facts_block(root)
    net_rule = ("# 网络限制(必须遵守)\n本题环境**禁止联网**:不允许上网搜索、不允许访问任何与题目无关的外部服务"
                "(搜索引擎/写题解/在线工具都不行;`pip install`/`apt install`/`curl` 外网同样会被拒,别浪费时间尝试)。\n"
                "缺工具时的正确做法:用本机已有工具、查知识库、或自己写脚本实现(本机 python3 库很全)。\n"
                "你只能使用:本机文件、本机工具(pwn64 虚拟机)、本地知识库,以及题目本身给的目标地址。\n"
                if os.environ.get("NETWORK_MODE") == "local_only" else "")
    values = {
        "SLOT": slot, "TOTAL": total, "ANGLE": ANGLES.get(slot, ANGLES[1]),
        "SQUAD_BLOCK": (("**你被派来增援,本次的专门分工是:" + str(q["squad_task"]) + "**\n"
                         "只做这一块,做完把结果写进 SHARED.md,不要重复其他 agent 的工作。\n\n")
                        if q.get("squad_task") else ""),
        "ATTEMPT": attempt, "WORKER_TIMEOUT": worker_timeout or 420,
        "MAX_HELPERS": PROF.dispatch_int("max_helpers", "MAX_HELPERS_PER_QUESTION", 3),
        "QID": q["question_id"], "TITLE": q.get("title", ""), "CATEGORY": q.get("category", ""),
        "SCORE": q.get("score", 0), "DESCRIPTION": q.get("description", ""),
        "DIFFICULTY": q.get("difficulty") or "-", "LEVEL": q.get("level") or "-",
        "FLAG_COUNT": flag_count, "PROGRESS": progress,
        "ATTRIBUTES": q.get("attributes"), "CAPABILITIES": q.get("capabilities"),
        "EXTENSIONS_BLOCK": (f"- 扩展信息: {q.get('extensions')}\n" if q.get("extensions") else ""),
        "CONNECTION_BLOCK": _connection_block(q.get("connection") or {}),
        "WDIR": wdir, "FILES": root / "files", "SHARED": root / "SHARED.md", "ROOT": root,
        "RUN_DIR": RUN_DIR,
        "PLAYBOOK": category_playbook(q.get("category", "")) or "(本方向暂无节选,可用 kb.py list 查看)",
        "ENV_HINT": _env_hint(q.get("category", "")),
        "PLATFORM_NOTE": platform_note(),
        "PLATFORM_TOOLS": platform_tools_note(),
        "KB_HINT": kb_hint, "HINT_BLOCK": hint_block, "TIME_RULE": _time_rule(),
        "SHARED_FACTS": shared_facts,
        "NET_RULE": net_rule,
        "KB_TOOL": ROOT / "tools" / "kb.py",
        "JOB_TOOL": ROOT / "tools" / "job.py",
        "HINT_TOOL": ROOT / "tools" / "get_hint.py",
        "SUBMIT_TOOL": SUBMIT_TOOL, "RESET_TOOL": RESET_TOOL, "REPORT_TOOL": REPORT_TOOL,
        "RSA_TOOL": ROOT / "knowledge" / "vendor" / "RsaCtfTool" / "RsaCtfTool.py",
    }
    return _expand(prompt_template(), values)


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
        "--no-session", "--no-skills",
        "--no-prompt-templates", "--no-context-files",
    ]
    # 策略包声明的 pi 扩展(如涉诈包的出口合规闸门)+ 默认的 AI 调用追踪扩展。
    # 始终 --no-extensions:只加载显式声明的,避免 ~/.pi 下的用户扩展混进来影响跑分。
    exts = list(PROF.extensions())
    if os.environ.get("ROTOM_AI_TRACE", "1") != "0":
        _trace = ROOT / "tools" / "pi_extensions" / "ai_trace.ts"
        if _trace.exists() and str(_trace) not in exts:
            exts.append(str(_trace))
    for ext in exts:
        cmd += ["--extension", ext]
    cmd.append("--no-extensions")
    sp = system_prompt_file()          # 策略包专属系统提示词(身份/红线/自主性),系统层强约束
    if sp is not None:
        cmd += ["--append-system-prompt", str(sp)]
    cmd.append(build_prompt(q, slot, total, wdir, attempt=attempt, worker_timeout=timeout))
    child_env = {**os.environ, "TEAM_TOKEN": env["TEAM_TOKEN"]}
    # AI 调用追踪:扩展把每次 provider 请求的起止/用量写到与 worker 日志同名的 .calls 文件
    child_env["ROTOM_AI_TRACE_FILE"] = str(log_path) + ".calls"
    # 统一编码:从源头减少 GBK 输出(agent 把工具输出 >> 进黑板时最容易踩;实测 0xbc 字节把 runner 打崩过)
    child_env["LC_ALL"] = child_env["LANG"] = "en_US.UTF-8"
    child_env["PYTHONUTF8"] = "1"                   # Python 子进程走 UTF-8 模式
    child_env["PYTHONIOENCODING"] = "utf-8"
    if env.get("RUN_DEADLINE_TS"):      # 让 worker 知道本场还剩多久(提示词里的时间预算靠它)
        child_env["RUN_DEADLINE_TS"] = env["RUN_DEADLINE_TS"]
    if env.get("KB_EXTRA"):             # 上一轮战果目录 → worker 里 kb.py 也能查到(测试/自查都方便)
        child_env["KB_EXTRA"] = env["KB_EXTRA"]
    if env.get("FALSE_NEGATIVE_RETRIES"):
        child_env["FALSE_NEGATIVE_RETRIES"] = env["FALSE_NEGATIVE_RETRIES"]
    # 把 tools/bin 放到 PATH 最前:macOS 没有 timeout,这里提供了替身,agent 才能给长命令限时
    child_env["PATH"] = f"{ROOT / 'tools' / 'bin'}:{child_env.get('PATH', '')}"
    # ---- 代理处理(实测教训:本机代理一旦失效,模型请求全挂;所以模型端点必须绕过代理) ----
    # 1) 白名单始终包含:本机 + 模型端点 + 跑分平台/靶场域名 + 本题目标主机
    allow = ["localhost", "127.0.0.1", "::1",
             "api.deepseek.com", "api.kimi.com", "www.micuapi.ai"]
    allow += _benchmark_host()                               # 跑分平台域名
    allow += [h for h in (env.get("NET_ALLOW") or "").split(",") if h]
    allow += _provider_hosts(env.get("PI_PROVIDER", ""))     # 从 pi 的 models.json 读当前 provider 的域名
    conn = q.get("connection") or {}
    for v in list(conn.values()):          # 题目容器/目标地址必须放行
        v = str(v)
        host = v.split("//")[-1].split("/")[0].split(":")[0].replace("nc ", "").strip()
        # 正式渗透的目标常是裸 IP/网段(没有域名),也要放行;其余模式沿用旧行为(只放行域名)
        if host and (PROF.goal == "findings" or not host[0].isdigit()):
            allow.append(host)
    no_proxy = ",".join(dict.fromkeys(allow))
    cur = child_env.get("no_proxy", "")
    child_env["no_proxy"] = child_env["NO_PROXY"] = (cur + "," + no_proxy).strip(",")

    # 2) 网络策略(直连摘掉代理变量 / 断网走黑洞):语义统一在 config.apply_net_policy
    config_mod.apply_net_policy(child_env, env)

    # 跑分平台域名强制直连:本机代理会破坏其 HTTPS(实测教训)
    bypass = ",".join(_benchmark_host())
    if bypass:
        for key in ("no_proxy", "NO_PROXY"):
            cur = child_env.get(key, "")
            child_env[key] = (cur + "," + bypass).strip(",") if bypass not in cur else cur
    if env.get("PI_API_KEY_VAR") and env.get("PI_API_KEY"):
        child_env[env["PI_API_KEY_VAR"]] = env["PI_API_KEY"]
    proc = subprocess.Popen(
        cmd, cwd=wdir, stdout=log_f, stderr=subprocess.STDOUT,
        env=child_env, start_new_session=True,  # 便于超时整组杀掉
    )
    proc._agent_log_f = log_f  # type: ignore[attr-defined]
    proc._agent_log_path = log_path  # type: ignore[attr-defined]
    return proc


def _brief(cmd: str, n: int = 70) -> str:
    """把一条命令压成好看的摘要。

    agent 习惯用 `cd <工作目录> && ...` 开头,直接截断的话摘要全是路径、看不出在干什么
    (实测:黑板里 40 条 Observer 全是 `bash(cd <工作目录>)`)。
    所以先剥掉 cd 前缀,再截断。
    """
    s = " ".join(str(cmd).split())
    s = re.sub(r"^cd\s+\S+\s*(?:&&|;)\s*", "", s)
    s = re.sub(r"^cd\s+\S+\s+", "", s)
    # 非 cd 开头的命令里也会带长绝对路径(如 `ls -la <工作目录>`),同样吃掉
    s = re.sub(r"(?:/[\w\u4e00-\u9fff_.@-]+){4,}/", "…/", s)
    return s[:n]


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
            brief = _brief(args.get("command") or args.get("path") or args.get("pattern") or "")
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


def tool_signatures(log_path: Path, k: int = 40, max_bytes: int = 400_000) -> tuple[int, str]:
    """最近 k 次工具调用里的"重复特征":返回 (最高重复次数, 那条命令的摘要)。

    这是**被实测验证过的"真卡住"信号**:CyberStrike 对"连续 N 步无产出"做过对照(XBEN-025),
    连编排者都会被误催(~70%),已弃用;而 (工具, 参数) **字节级重复**不会误伤正常探测 ——
    `id=1`/`id=2`/`id=3` 是不同签名,只有真把同一条命令又跑一遍才计数。
    参数只做空白归一化(换行/多余空格),不做模糊匹配。
    """
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
        return 0, ""
    sigs: list[str] = []
    for line in lines:
        try:
            ev = _json.loads(line)
        except Exception:
            continue
        if ev.get("type") != "tool_execution_start":
            continue
        args = ev.get("args") or {}
        raw = str(args.get("command") or args.get("path") or args.get("pattern") or "")
        sig = " ".join(f"{ev.get('toolName', '?')}|{raw}".split())
        if sig:
            sigs.append(sig)
    if not sigs:
        return 0, ""
    cnt: dict[str, int] = {}
    for s in sigs[-k:]:
        cnt[s] = cnt.get(s, 0) + 1
    top, n = max(cnt.items(), key=lambda kv: kv[1])
    return n, _brief(top.split("|", 1)[-1], 90)


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
#
# ⚠ 这个"错误语义"标记本身**也不能用裸词/裸数字**(2026-10-09 第九轮实测的假阳性):
# 原先这里是 `error|exception|failed|failure|…|\b4\d\d\b|\b5\d\d\b` ——
# 而渗透题的面板/端口/状态码散文里到处都是 "error"、403、404、500、502;
# 于是模型自己写 "rate limit. But we should avoid heavy brute force" 时,±160 字窗口里恰好命中
# → 被判成"模型侧限流" → 并发降档(5→4)+该题退避重试(那一轮 03:30 就是这么来的)。
# 口径改成"**结构化报错形状**":HTTP 状态行、JSON 的 code/error 字段、traceback、retry-after、
# pi 的 isError 标记 —— 模型散文里不会自然长成这些形状。
ERROR_MARKERS = re.compile(
    r"HTTP/\d(?:\.\d)?[\"']?\s*[,:]?\s*\d{3}\b|"                 # HTTP/1.1 429
    r"\bstatus(?:_?code)?[\"'\s:=]+\d{3}\b|"                       # status_code: 429 / "status":429
    r"\"(?:code|error|errorMessage|error_type|error_code)\"\s*[:=]|"  # JSON 报错字段
    r"isError\"?\s*[:=]\s*true|"                                     # pi 的 isError
    r"\b(?:Type|Value|Key|HTTP|RateLimit|Connection)(?:Error|Exception)\b\s*[:：]|"
    r"\btraceback\b|\bretry[-_]?after\b|"                            # 堆栈 / 限流响应头
    r"\b(?:429|503|529)\b[^\n]{0,30}(?:Too Many|unavailable|overload|rate|throttl)",
    re.IGNORECASE)


def _hits_with_error_context(rx: re.Pattern, text: str, window: int = 160) -> bool:
    """关键词命中且**附近出现错误语义**才算数(避免把模型散文当成服务端报错)。"""
    for m in rx.finditer(text):
        seg = text[max(0, m.start() - window): m.end() + window]
        if ERROR_MARKERS.search(seg):
            return True
    return False


def _error_segments(tail: str) -> tuple[str, str]:
    """从日志尾部挑出"**服务端/工具报错**"的文本,返回 (pi 自己的 error 事件文本, 工具 isError 结果文本)。

    为什么必须按**事件类型**先过滤,而不是直接扫整个 tail(2026-10-09 第九轮实测的假阳性):
    worker 日志里大段是**模型自己的思考** —— 做 web 题时它会反复写 "rate limit"、"avoid heavy brute force",
    还会写 403/404/500 这些状态码;只要关键词 + 上下文标记里掺进裸词/裸数字,扫全文必然误伤
    (那一轮 03:30 的"检测到模型侧限制 → 并发降档 5→4"就是这么来的,而且它与真实情况无关:
    模型只是在**计划**怎么避免目标侧的限流)。正确做法:只有 pi 的 `error` 事件
    与 `isError=true` 的工具结果才算"错误载体",模型散文永远进不来。
    """
    import json as _json     # 本模块惯例:函数内 import(见上面 providers/messages 那几处)

    own: list[str] = []      # pi 自己抛的错(API 层):文本本身就是错误,无需再找标记
    tool: list[str] = []     # 工具报错:可能夹带 agent 写的文件内容,仍需"错误语义"二次确认
    for line in tail.splitlines():
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            ev = _json.loads(s)          # 注意用本模块的别名 `_json`(这里没有裸 `json`;写错会被 except 吞掉)
        except Exception:                                     # noqa: BLE001  半截行/非 JSON
            continue
        et = ev.get("type")
        if et == "error":
            own.append(str(ev.get("errorMessage") or ev.get("message") or ""))
        elif et == "agent_end":
            # pi 在 API 连续失败、自动重试用尽后,以 agent_end + stopReason=error 收场,
            # **不**另发 type=error 事件(2026-10-28 实测:同一轮 270 次 429 全落在这里)。
            # 漏掉这一支的后果:限流退出被当成"正常结束(exit=0)"→ 立刻满并发重开 → 持续撞限流。
            for m in (ev.get("messages") or []):
                if isinstance(m, dict) and m.get("stopReason") == "error":
                    own.append(str(m.get("errorMessage") or ""))
        elif et == "message_end":
            m = ev.get("message") or {}
            if m.get("role") == "toolResult" and m.get("isError"):
                tool.append(" ".join(c.get("text", "") for c in (m.get("content") or [])
                                     if isinstance(c, dict)))
        elif et == "tool_execution_end" and ev.get("isError"):
            r = ev.get("result") or {}
            if isinstance(r, dict):
                tool.append(" ".join(c.get("text", "") for c in (r.get("content") or [])
                                     if isinstance(c, dict)))
    return "\n".join(own), "\n".join(tool)


def classify_failure(log_path: Path, max_bytes: int = 200_000) -> str | None:
    """读 worker 日志尾部,判断是否为模型侧限制。返回 None/'rate_limit'/'quota'/'net'。

    只分析 `_error_segments()` 挑出来的报错载体 —— 见那里的注释(别再退回"扫全文 + 关键词")。
    """
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            tail = f.read().decode("utf-8", "ignore")
    except OSError:
        return None
    own_err, tool_err = _error_segments(tail)
    if not own_err and not tool_err:
        return None            # 一条报错载体都没有 → 不可能是模型侧限制(退出是超时/被信号杀)
    for rx, tag in ((QUOTA_PATTERNS, "quota"), (RATE_LIMIT_PATTERNS, "rate_limit")):
        if rx.search(own_err):                      # pi 的 error 事件:文本即错误
            return tag
        if _hits_with_error_context(rx, tool_err):   # 工具报错:再过一遍错误语义
            return tag
    if _hits_with_error_context(NETWORK_PATTERNS, own_err + "\n" + tool_err, window=80):
        return "net"           # 模型端点连不上:退避重试,别当题目失败
    return None
