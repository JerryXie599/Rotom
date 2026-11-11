#!/usr/bin/env python3
"""本地控制台 + 看板(零依赖,只用 Python 标准库)。

  python3 dashboard.py              # 默认 http://127.0.0.1:8788
  python3 dashboard.py --port 9000

功能:
  1. 控制面板:在网页里改队伍 token、模型 base URL / key / 模型名 / API 类型,
     以及起始并发、练习模式;保存即写入 .env 并同步成 pi 的 provider。
  2. 启停 runner:启动 / 停止 / 重启(停止会优雅收掉所有 worker)。
  3. 实时看板:每道题状态、各 agent 的实时动作与发言、共享黑板、提交记录、事件流。

只监听 127.0.0.1,数据直接读 board.json / logs/,不经过任何外部服务。
"""

from __future__ import annotations

import argparse
import glob
import datetime
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import config as config_mod  # noqa: E402
import textutil  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
try:                      # 复用评审器的材料指纹 + 口径版本,保证"待办"口径与真跑一致
    from review_results import material_sig as _rv_material_sig, REV as _rv_rev  # noqa: E402
except Exception:  # noqa: BLE001
    _rv_material_sig = None
    _rv_rev = 0

PROJECTS_DIR = ROOT / "projects"
DEFAULT_PROJECT = "默认项目"
SHEZHA_PROJECT = "涉诈批次"          # 伪项目:数据目录=涉诈仓库的 00_总控/自动批次

STATUS_LABEL = {"pending": "待派发", "running": "运行中", "solved": "已解出", "failed": "已放弃"}
STATUS_CLASS = {"pending": "warn", "running": "run", "solved": "ok", "failed": "bad"}

EDITABLE_KEYS = ("PROFILE", "CONTEST_ADAPTER", "BENCHMARK_BASE_URL", "BENCHMARK_TOKEN", "VPN_CHECK_URL",
                 "PI_BASE_URL", "PI_API_KEY", "PI_MODEL", "PI_API_TYPE",
                 "START_WORKERS", "MAX_ACTIVE_CHALLENGES", "QUESTION_SLICE_SECONDS",
                 "WORKER_TIMEOUT", "WORKER_MIN_LIFETIME", "ADOPT_WORKERS", "ROTATE_WAIT_RUNNING",
                 "ROUND_WINDOW_MINUTES", "HINT_AFTER_ATTEMPTS", "HINT_EARLY_DIFFS", "HINT_SKIP_DIFFS",
                 "API_FAIL_RELOG_S", "STALL_WARN_S", "STALL_MIN_PENDING",
                 "IGNORE_SOLVED", "NET_PROXY")
API_TYPES = ("openai-completions", "anthropic-messages", "openai-responses", "google-generative-ai")


# ---------------------------------------------------------------- 项目管理

def slugify(name: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in name.strip()]
    out = "".join(keep).strip("-")
    return out or "project"


def project_dir(name: str) -> Path:
    """每个项目一个独立数据目录:默认项目用代码根目录(兼容已有数据),其它在 projects/<slug>/。"""
    if name == DEFAULT_PROJECT:
        return ROOT
    return PROJECTS_DIR / slugify(name)


def list_projects() -> list[dict]:
    out = [{"name": DEFAULT_PROJECT, "dir": str(ROOT), "created": None,
            "builtin": True, "questions": _count_questions(ROOT), "profile": ""}]
    sz = shezha_state()
    if sz.get("configured") and sz.get("ready"):
        # 涉诈批次伪项目:board 里的每个"题"就是一个涉诈站点,左侧栏/详情页整套复用
        out.append({"name": SHEZHA_PROJECT, "dir": sz["workdir"], "created": None,
                    "builtin": True, "questions": _count_questions(Path(sz["workdir"])),
                    "profile": "shezha"})
    if PROJECTS_DIR.exists():
        for d in sorted(PROJECTS_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            meta = read_json(d / "meta.json", {})
            out.append({"name": meta.get("name") or d.name, "dir": str(d),
                        "created": meta.get("created"), "builtin": False,
                        "questions": _count_questions(d),
                        "profile": meta.get("profile") or ""})
    return out


def _count_questions(rundir: Path) -> int:
    board = read_json(rundir / "board.json", {})
    return len(board.get("questions") or {})


def find_project(name: str) -> dict | None:
    for pr in list_projects():
        if pr["name"] == name:
            return pr
    return None


def project_profile(name: str) -> str:
    """项目生效的策略包:meta.json 里的 profile → 全局 .env 的 PROFILE → 空(用默认包)。"""
    pr = find_project(name)
    if pr and pr.get("profile"):
        return str(pr["profile"])
    cfg = {**config_mod.DEFAULTS, **config_mod.read_env()}
    return (cfg.get("PROFILE") or "").strip()


def create_project(name: str, profile: str = "") -> str:
    name = (name or "").strip()
    if not name:
        return "项目名不能为空"
    if name == DEFAULT_PROJECT or find_project(name):
        return f"项目已存在: {name}"
    profile = (profile or "").strip().lower()
    if profile:
        try:                                     # 名字写错会让 runner 起不来,先挡住
            import profile as profile_mod
            profile_mod.load(profile)
        except Exception as e:
            return f"策略包不可用({profile}): {e}"
    d = project_dir(name)
    if d.exists() and any(d.iterdir()):
        return f"目录已存在且非空: {d}"
    for sub in ("work", "logs"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps({"name": name, "created": time.strftime("%F %T"), "profile": profile},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")
    tail = f"(策略包 {profile})" if profile else "(策略包跟随全局设置)"
    return f"已创建项目「{name}」{tail} → {d}"


def delete_project(name: str) -> str:
    """删除一个项目(数据目录整体删除)。**默认项目/内置项目不可删**,runner 在跑时先停再删。"""
    name = (name or "").strip()
    if not name:
        return "项目名不能为空"
    if name == DEFAULT_PROJECT:
        return "默认项目不可删除"
    pr = find_project(name)
    if not pr or pr.get("builtin"):
        return f"项目不存在: {name}"
    d = Path(pr["dir"])
    if runner_status(d)["running"]:
        return f"项目「{name}」的 runner 正在跑 —— 先点「停止」再删"
    try:
        import shutil as _shutil
        _shutil.rmtree(d)
    except OSError as e:
        return f"删除失败:{e}"
    return f"已删除项目「{name}」(数据目录 {d} 一并删除)"


def apply_env() -> None:
    """把 .env 的值注入本进程环境变量,供 contest_api 等模块读取。"""
    for k, v in config_mod.read_env().items():
        if v != "":
            os.environ[k] = v


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def pid_alive(pid) -> bool:
    """进程存活校验:避免 runner 被强杀后残留注册记录显示成幽灵 worker。"""
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


# ---------------------------------------------------------------- 控制面板后端

def runner_status(rundir: Path) -> dict:
    pid_file = rundir / "logs" / "runner.pid"
    pid = None
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
        except ValueError:
            pid = None
    if pid:
        # dashboard 自己拉起的 runner 死后若还没被 reap,会以僵尸态骗过 kill(pid,0);
        # WNOHANG 收割一把(在跑返回 (0,0) 无副作用;非本进程子进程报 ECHILD 忽略)。
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
    running = bool(pid and pid_alive(pid))
    if pid_file.exists() and not running:
        pid_file.unlink(missing_ok=True)
    return {"running": running, "pid": pid}


# ---- EXTERNAL_RUNNER:数据目录被"外部 runner"占着(容器/别的机器在跑),控制台不代管 ----
# 为什么:容器模式(本地整轮容器、或平台托管)下,数据目录是**挂载在宿主机上的**,控制台能读到它 ——
# 若有人在这里点「开始跑」,就会在本机再起一个 runner,同一个 token + 同一份 board 双跑(踩过同 token 双跑的坑)。
# 由启动方(如"跑一轮.sh")在 logs/EXTERNAL_RUNNER.json 写一行 {container|host, ts};收工时删掉。
EXTERNAL_RUNNER = "EXTERNAL_RUNNER.json"


def external_runner(rundir: Path) -> str:
    """返回"外部 runner 在跑"的说明;没有(或已停)则返回空串。

    判据两道:① 标记文件在;② **心跳是新鲜的** —— runner_state.json 的 heartbeat ≤ 180 秒。
    只看文件的话,容器停了标记还留着,控制台会一直宣称"在跑"、把题显示成运行中;
    跟着心跳失效之后,收工/换地方跑都能自动回到正常行为(标记文件留着不影响,下次起容器会重写)。
    """
    p = rundir / "logs" / EXTERNAL_RUNNER
    if not p.exists():
        return ""
    hb = (read_json(rundir / "logs" / "runner_state.json", {}) or {}).get("heartbeat")
    try:
        if not hb or (time.time() - float(hb)) > 180:
            return ""
    except (TypeError, ValueError):
        return ""
    meta = read_json(p, {}) or {}
    who = (meta.get("container") or meta.get("host") or "").strip() or "外部"
    how = f"docker stop {who}" if meta.get("container") else f"在 {who} 上停掉它"
    return f"该项目由**外部 runner** 在跑({who})——控制台不代管:看日志/收工请用 {how};要在这里跑,先删掉 logs/{EXTERNAL_RUNNER}"


def start_runner(project: str) -> str:
    pr = find_project(project)
    if not pr:
        return f"项目不存在: {project}"
    rundir = Path(pr["dir"])
    st = runner_status(rundir)
    if st["running"]:
        return f"「{project}」的 runner 已在运行 (pid {st['pid']})"
    ext = external_runner(rundir)          # EXTERNAL_RUNNER:容器/别的机器在跑 → 拒绝本机再起一个
    if ext:
        return ext
    env = {**os.environ, "ROTOM_RUN_DIR": str(rundir)}  # 数据目录指向本项目,项目之间互不干扰
    env.update({k: v for k, v in config_mod.read_env().items() if v != ""})
    (rundir / "logs").mkdir(parents=True, exist_ok=True)
    out = open(rundir / "logs" / "runner.out", "a", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(ROOT / "runner.py")], cwd=ROOT, env=env,
                            stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(30):  # 等 runner 写 pid 文件
        time.sleep(0.2)
        if (rundir / "logs" / "runner.pid").exists():
            break
    return f"「{project}」已开始跑 (pid {proc.pid}),工作区 {rundir}"


def stop_runner(project: str) -> str:
    pr = find_project(project)
    if not pr:
        return f"项目不存在: {project}"
    rundir = Path(pr["dir"])
    ext = external_runner(rundir)          # EXTERNAL_RUNNER:别用控制台的停止去动外部 runner
    if ext:
        return ext
    st = runner_status(rundir)
    if not st["running"]:
        return f"「{project}」的 runner 未在运行"
    try:  # SIGTERM 会让 runner 走优雅退出分支,把 worker 一起收掉
        os.killpg(st["pid"], signal.SIGTERM)
    except Exception:
        try:
            os.kill(st["pid"], signal.SIGTERM)
        except Exception as e:
            return f"停止失败: {e}"
    for _ in range(40):
        time.sleep(0.25)
        if not runner_status(rundir)["running"]:
            return f"「{project}」已停止,worker 已清理"
    return "已发送停止信号,但进程仍在退出中"


# ================= 涉诈自动批次(shezha)=================
# 作战仓库根(…/渗透/涉诈)存 .env 的 SHEZHA_ROOT;网页里绑定一次,之后
# Excel 上传/切批次/开批次/收工全部在页面点,不再走命令行。
SHEZHA_DEF_PROVIDER = "bigmodel-shezha"
SHEZHA_DEF_MODEL = "GLM-5.3-Flash"


def _shezha_cfg() -> dict:
    try:
        return config_mod.read_env()
    except Exception:
        return {}


def shezha_state() -> dict:
    root = (_shezha_cfg().get("SHEZHA_ROOT") or "").strip()
    if not root:
        return {"configured": False}
    rp = Path(root).expanduser()
    ws = rp / "00_总控" / "自动批次"
    cfg = _shezha_cfg()
    st: dict = {"configured": True, "root": str(rp), "workdir": str(ws),
                "ready": ((ROOT / "tools" / "excel_targets.py").is_file()
                          and (ws / "config.json").is_file()
                          and (ws / "开批次.sh").is_file()),
                "runner": runner_status(ws),
                "pi_provider": cfg.get("SHEZHA_PI_PROVIDER") or SHEZHA_DEF_PROVIDER,
                "pi_model": cfg.get("SHEZHA_PI_MODEL") or SHEZHA_DEF_MODEL}
    tj = read_json(ws / "targets.json", {})
    targets = tj.get("targets") or []
    m = re.search(r"批次(\d+)", str(tj.get("engagement") or ""))
    st["batch"] = int(m.group(1)) if m else None
    st["engagement"] = str(tj.get("engagement") or "")
    st["targets"] = [{"id": t.get("id"),
                      "src": (t.get("notes") or "").split(";")[0].replace("来源:", "")}
                     for t in targets]
    st["lane"] = None
    if st["batch"]:
        for p in sorted(rp.glob("LANE_CLAIM_批次*.md"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            if p.stem.rstrip(".md").endswith(f"批次{st['batch']}"):
                st["lane"] = p.name
                break
    board = read_json(ws / "board.json", {})
    qs = board.get("questions") or {}
    st["progress"] = {"total": len(qs),
                      "solved": sum(1 for e in qs.values()
                                    if e.get("delivered") or e.get("is_solved")),
                      "findings": sum(int(e.get("findings_count") or 0) for e in qs.values())}
    st["close"] = read_json(ws / "logs" / "close_status.json", None)
    st["review"] = read_json(ws / "logs" / "review_status.json", None)
    st["autosync"] = _sz_sync_state(ws)
    st["autosync_on"] = str(cfg.get("SHEZHA_AUTOSYNC", "1")).strip() not in ("0", "off", "false")
    return st


def shezha_config(payload: dict) -> str:
    root = str(payload.get("root") or "").strip()
    if not root:
        return "✘ 路径不能为空"
    rp = Path(root).expanduser()
    ws = rp / "00_总控" / "自动批次"
    if not (ws / "excel转targets.py").is_file():
        return f"✘ {ws} 下没找到 excel转targets.py —— 确认填的是涉诈作战仓库根目录"
    updates = {"SHEZHA_ROOT": str(rp)}
    if payload.get("pi_provider"):
        updates["SHEZHA_PI_PROVIDER"] = str(payload["pi_provider"]).strip()
    if payload.get("pi_model"):
        updates["SHEZHA_PI_MODEL"] = str(payload["pi_model"]).strip()
    config_mod.write_env(updates)
    apply_env()
    return f"已绑定作战仓库:{rp}"


def shezha_residents(site: str | None = None) -> dict:
    """解析常驻登记表(唯一真源),返回行级明细 + 按站点聚合;site 给定时按子串过滤。

    PID 活性是**实况校验**(kill(pid,0)),与登记状态列互相印证:登记"运行中"但 PID 死
    = 僵尸行;PID 活但登记"已清理" = 表外进程(开工纪律要查清来路)。
    """
    st = shezha_state()
    if not st.get("configured"):
        return {"rows": [], "groups": []}
    reg = Path(st["root"]) / "00_总控" / "常驻登记表.md"
    rows: list[dict] = []
    try:
        text = reg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"rows": [], "groups": []}
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.replace("*", "").strip() for c in s.strip("|").split("|")]
        if len(cells) < 8 or cells[0] in ("", "名称") or set(cells[0]) <= set("-: "):
            continue
        # 「批次N_对账/盘点/审计」这类行是登记表里的批次日志,不是常驻脚本,不算
        if re.search(r"(对账|盘点|审计|复盘)", cells[0]):
            continue
        m = re.search(r"\d{2,}", cells[4] or "")
        pid = int(m.group()) if m else None
        rows.append({"name": cells[0], "site": cells[1], "use": cells[2], "path": cells[3],
                     "pid": cells[4], "pid_alive": bool(pid and pid_alive(pid)),
                     "started": cells[5], "log": cells[6], "status": cells[7],
                     "stopped": cells[8] if len(cells) > 8 else ""})
    if site:
        key = site.lower()
        tokens = [t for t in re.split(r"[.\-_]", key) if len(t) >= 3]
        rows = [r for r in rows
                if key in r["site"].lower() or any(t in r["site"].lower() for t in tokens)]
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(r["site"], {"site": r["site"], "total": 0, "running": 0})
        g["total"] += 1
        if r["status"].startswith("运行中"):
            g["running"] += 1 if r["pid_alive"] else 0
            g.setdefault("zombie", 0)
            if not r["pid_alive"]:
                g["zombie"] += 1
    running = [r for r in rows if r["status"].startswith("运行中")]
    return {"rows": rows,
            "running": len(running),
            "zombie": sum(1 for r in running if not r["pid_alive"]),
            "groups": sorted(groups.values(), key=lambda g: -g["running"] * 1000 - g["total"])}


def shezha_site_payload(entry: dict) -> dict | None:
    """涉诈站点的档案视图:00_档案.md 全文 + 报告/证据/导图文件清单 + 该站常驻。

    entry.workdir = <档案>/<域名>/工作区 → 上级就是站点档案文件夹。
    """
    wd = (entry.get("workdir") or "").strip()
    if not wd:
        return None
    arch = Path(wd).expanduser().parent
    if not (arch / "证据").exists() and not arch.exists():
        return None

    def _read(p: Path, cap: int = 12000) -> str:
        try:
            return p.read_text(encoding="utf-8", errors="replace")[:cap]
        except OSError:
            return ""

    def _list(d: Path) -> list[str]:
        try:
            return sorted(p.name for p in d.iterdir() if p.is_file())
        except OSError:
            return []

    domain = entry.get("question_id") or arch.name
    res = shezha_residents(domain)
    return {
        "archive": str(arch),
        "profile_md": _read(arch / "00_档案.md"),
        "findings_md": _read(arch / "工作区" / "FINDINGS.md", 6000),
        "reports": _list(arch / "报告"),
        "evidence": _list(arch / "证据")[-30:],
        "mindmaps": [n for n in _list(arch) + _list(arch / "报告") if n.startswith("思维导图")],
        "residents": res["rows"],
    }


def shezha_agent_map(inspect: dict, files: list[str]) -> dict | None:
    """Agent 介入认列:把各 sheet 的表头/样例/每列域名密度交给模型,要一份列映射 JSON。

    返回 {"Sheets": {"<sheet名>": {"include": true, "url_cols": [2], "note_cols": [3]}}}
    形状的数据(容错:取模型输出里第一个 {...} 块);失败返回 None(回退自动识别)。
    """
    cfg = {**config_mod.DEFAULTS, **config_mod.read_env()}
    provider = cfg.get("SHEZHA_PI_PROVIDER") or SHEZHA_DEF_PROVIDER
    model = cfg.get("SHEZHA_PI_MODEL") or SHEZHA_DEF_MODEL
    prompt = (
        "你在帮一个涉诈网站侦查项目解析目标榜单 Excel。下面是每个 sheet 的结构探测结果"
        "(key、表头、前几行样例、每列提取到的域名/IP 计数)。\n"
        "请判断每个 sheet 要不要纳入、哪几列是目标列(URL/域名/IP,可能不止一列)、"
        "哪几列是备注列(提取成 notes)。注意区分『目标网址列』和『处置状态/备注里的附带域名』"
        "——状态列(如 探测结果/处置意见/失败原因)即使含域名也不是目标列。\n"
        "只输出一个 JSON 对象,不要任何解释,格式:\n"
        '{"Sheets": {"<key 原样>": {"include": true, "url_cols": [1], "note_cols": [2,3]}}}\n'
        "key 必须逐字用材料里给出的 key 字段。列号从 0 开始。"
        "不想纳入的 sheet 写 {\"include\": false};没给出的 sheet 默认 include=false。\n\n"
        + json.dumps(inspect, ensure_ascii=False)[:14000])
    try:
        r = subprocess.run(
            ["pi", "-p", "--provider", provider, "--model", model, "--no-session",
             "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files", prompt],
            capture_output=True, text=True, timeout=180)
    except Exception:
        return None
    m = re.search(r"\{.*\}", (r.stdout or ""), re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    sheets = data.get("Sheets") or data.get("sheets")
    return sheets if isinstance(sheets, dict) and sheets else None


def shezha_excel(payload: dict) -> tuple[str, str, dict]:
    """上传 Excel → 结构探测 →(歧义时 Agent 认列)→ 切批次。返回 (错误, 输出, 元信息)。

    返回 (错误, 输出, 元信息)。自动识别的口径:某 sheet 恰有一列域名/IP 密度显著高于其余列 → 直接用;
    多列都有域名/全是文本混杂 → 交给模型认列(返回 used_agent=true)。
    """
    st = shezha_state()
    if not st.get("configured"):
        return "先绑定作战仓库根目录", "", {}
    ws = Path(st["workdir"])
    files = payload.get("files") or []
    if not files:
        return "没有收到 Excel 文件", "", {}
    import base64
    updir = ws / "uploads"
    updir.mkdir(parents=True, exist_ok=True)
    paths = []
    for f in files[:5]:
        name = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", str(f.get("name") or "榜单.xlsx"))
        p = updir / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}"
        try:
            p.write_bytes(base64.b64decode(str(f.get("data_b64") or "")))
        except Exception as e:
            return f"文件解码失败({name}): {e}", "", {}
        paths.append(str(p))
    script = ROOT / "tools" / "excel_targets.py"      # 机制在 Agent 仓库;策略在项目 config.json
    cfg_arg = ["--config", str(ws / "config.json")]
    meta: dict = {"used_agent": False, "mapping": None}
    # 1) 结构探测(自动识别的输入,也是 Agent 认列的输入)
    try:
        ins = subprocess.run([sys.executable, str(script), *cfg_arg, "--inspect", *paths],
                             capture_output=True, text=True, timeout=180)
        inspect = json.loads((ins.stdout or "{}").strip().splitlines()[-1]) if ins.returncode == 0 else {}
    except Exception:
        inspect = {}
    # 2) 歧义判定:任何 sheet 有 ≥2 列域名密度>0,或最高密度列占比不足 60%,就请模型认列
    need_agent = False
    for sh in (inspect.get("sheets") or []):
        dens = [c for c in (sh.get("col_tokens") or []) if c > 0]
        mx = max(dens) if dens else 0
        if len(dens) >= 2 and mx and sum(dens) - mx > mx * 0.3:
            need_agent = True
    mapping = None
    if need_agent and payload.get("agent") != "off":
        mapping = shezha_agent_map(inspect, paths)
        if mapping:
            meta.update(used_agent=True, mapping=mapping)
    # 3) 切批次(有映射按映射读列,没有就走转换器自带的逐行提取)
    cmd = [sys.executable, str(script), *cfg_arg, *paths,
           "--批次大小", str(max(1, min(200, int(payload.get("batch_size") or 10))))]
    if payload.get("batch_no"):
        cmd += ["--批次号", str(int(payload["batch_no"]))]
    if payload.get("lib"):
        cmd += ["--档案库", str(payload["lib"])]
    if payload.get("redo"):
        cmd += ["--redo", str(payload["redo"])]
    if mapping:
        cmd += ["--apply-map", json.dumps(mapping, ensure_ascii=False)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return "转换超时(180s)", "", meta
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if inspect:
        meta["inspect"] = inspect
    err = "" if r.returncode == 0 else f"转换失败(退出码 {r.returncode})"
    return err, out[-3500:], meta


def shezha_runner(action: str) -> str:
    """开/停涉诈批次。启动直接复用作战仓库的 开批次.sh(出口预检+env 注入都在里面,单一事实源)。"""
    st = shezha_state()
    if not st.get("configured"):
        return "先绑定作战仓库根目录"
    ws = Path(st["workdir"])
    if action == "start":
        if not read_json(ws / "targets.json", {}).get("targets"):
            return "targets.json 为空 —— 先上传 Excel 切批次"
        if st["runner"]["running"]:
            return f"runner 已在运行 (pid {st['runner']['pid']})"
        (ws / "logs").mkdir(parents=True, exist_ok=True)
        out = open(ws / "logs" / "runner.out", "a", encoding="utf-8")
        env = {**os.environ, "ROTOM_HOME": str(ROOT)}
        proc = subprocess.Popen(["bash", str(ws / "开批次.sh")], cwd=str(ws), env=env,
                                stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(60):        # 预检(出口探活)几秒内出结果
            time.sleep(0.2)
            st2 = runner_status(ws)
            if st2["running"]:
                return f"批次已开跑 (pid {st2['pid']})"
            if proc.poll() is not None:
                try:
                    tail = (ws / "logs" / "runner.out").read_text(encoding="utf-8", errors="replace")[-600:]
                except OSError:
                    tail = ""
                return f"启动失败(出口预检没过?): {tail}"
        return "已提交启动(预检中),状态区几秒内刷新"
    if action == "stop":
        stx = runner_status(ws)
        if not stx["running"]:
            return "runner 未在运行"
        try:
            os.killpg(stx["pid"], signal.SIGTERM)
        except Exception:
            try:
                os.kill(stx["pid"], signal.SIGTERM)
            except Exception as e:
                return f"停止失败: {e}"
        for _ in range(40):
            time.sleep(0.25)
            if not runner_status(ws)["running"]:
                return "已停止,worker 已清理"
        return "已发送停止信号,进程仍在退出中"
    return f"未知操作: {action}"


_close_lock = threading.Lock()


def shezha_close(batch: int, scribe: bool) -> str:
    """收工联动(重建总表+常驻对账+档案审计[+书记员写批次汇总])后台跑,状态进 close_status.json。"""
    st = shezha_state()
    if not st.get("configured"):
        return "先绑定作战仓库根目录"
    ws = Path(st["workdir"])
    b = batch or st.get("batch") or 0
    if not b:
        return "拿不到批次号:先切批次再收工"
    if st["runner"]["running"]:
        return "runner 还在跑 —— 先「停止」再收工"
    with _close_lock:
        if (read_json(ws / "logs" / "close_status.json", {}) or {}).get("running"):
            return "上一次收工还在进行中,等它结束"

    def _job() -> None:
        status = {"running": True, "batch": b, "scribe": scribe,
                  "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        (ws / "logs").mkdir(parents=True, exist_ok=True)
        (ws / "logs" / "close_status.json").write_text(
            json.dumps(status, ensure_ascii=False), encoding="utf-8")
        cmd = [sys.executable, str(ws / "收工联动.py"), "--批次", str(b)]
        if scribe:
            cmd.append("--scribe")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            status.update(rc=r.returncode, running=False,
                          finished=time.strftime("%Y-%m-%d %H:%M:%S"),
                          output=((r.stdout or "") + (r.stderr or ""))[-4000:])
        except Exception as e:  # noqa: BLE001
            status.update(rc=-1, running=False, output=repr(e))
        (ws / "logs" / "close_status.json").write_text(
            json.dumps(status, ensure_ascii=False), encoding="utf-8")

    threading.Thread(target=_job, daemon=True).start()
    return f"收工已开始(批次{b}{' · 含书记员' if scribe else ''}),状态区几秒内刷新"


# ---- Provider 档案库:模型配置存名册,下拉切换一键应用(参考 ZCode provider 设置) ----

PROVIDERS_FILE = ROOT / "providers.json"


def _load_providers() -> dict:
    return read_json(PROVIDERS_FILE, {})


def provider_list() -> dict:
    saved = []
    for name, e in _load_providers().items():
        key = str(e.get("PI_API_KEY") or "")
        saved.append({"name": name,
                      "base_url": e.get("PI_BASE_URL", ""), "model": e.get("PI_MODEL", ""),
                      "api_type": e.get("PI_API_TYPE", "openai-completions"),
                      "key_mask": (key[:4] + "…" + key[-4:]) if len(key) > 10 else ("(空)" if not key else "***")})
    pi = []
    try:
        mj = json.loads((Path.home() / ".pi" / "agent" / "models.json").read_text(encoding="utf-8"))
        for name, e in (mj.get("providers") or {}).items():
            pi.append({"name": name, "models": [m.get("id") for m in (e.get("models") or []) if m.get("id")],
                       "base_url": e.get("baseUrl", "")})
    except (OSError, ValueError):
        pass
    return {"saved": saved, "pi": pi}


def provider_action(payload: dict) -> str:
    act = (payload.get("prov_action") or "").lower()
    name = str(payload.get("name") or "").strip()
    data = _load_providers()
    if act == "save":
        if not name or "/" in name:
            return "✘ 名字不能为空(且不能含 /)"
        data[name] = {"PI_BASE_URL": str(payload.get("PI_BASE_URL") or "").strip(),
                      "PI_API_KEY": str(payload.get("PI_API_KEY") or "").strip(),
                      "PI_MODEL": str(payload.get("PI_MODEL") or "").strip(),
                      "PI_API_TYPE": str(payload.get("PI_API_TYPE") or "openai-completions").strip()}
        PROVIDERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return f"已存 Provider「{name}」({data[name]['PI_MODEL'] or '未填模型'})"
    if act == "apply":
        e = data.get(name)
        if not e:
            return f"✘ 没有这个 Provider: {name}"
        cfg = {**config_mod.DEFAULTS, **config_mod.read_env()}
        cfg.update({k: v for k, v in e.items() if v})
        config_mod.write_env({k: v for k, v in e.items() if v})
        ok = config_mod.sync_provider(cfg, quiet=True)
        apply_env()
        return f"已应用「{name}」({e.get('PI_MODEL', '')})" + (";已同步 pi provider" if ok else "")
    if act == "delete":
        if name not in data:
            return f"✘ 没有这个 Provider: {name}"
        data.pop(name)
        PROVIDERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return f"已删除 Provider「{name}」"
    return f"未知操作: {act}"


# ---- 站点总库 / 簇划分 / 时间线 / 总Agent巡检(复用作战仓库已有沉淀) ----

_RESULT_RE = re.compile(
    r"拿到|拖库|拖取|打穿|拿下|攻破|shell|聊天记录|身份证|手机号|实名|整库|库表|"
    r"未鉴权.{0,6}(读|可读|泄露)|越权.{0,6}(读|可读)|信封|后台数据|收割", re.I)
# 「疑似正规资产」词:备案号/资质这类只能证明站点正规的证据,不算战果(与评审器 legit 档同口径)
_LEGIT_RE = re.compile(
    r"备案号|ICP备案|icp备案|联网备案|营业执照|经营资质|正规(站|平台|企业|公司|资产|域名|主体)|"
    r"合法(经营|主体|站点)|事业单位|已备案|知名平台", re.I)


_PLACEHOLDER_RE = re.compile(r"^[（(]?(待探测|暂无记录|暂无|待补|无|—|-|－|/|空)[)）]?$")
_MM_PLACEHOLDER_RE = re.compile(r"^\s*-\s*\((?:后续探测在此增删节点|待探测)\)\s*$", re.M)
_ARCHIVE_DIR_RE = re.compile(r"^(00_|归档|_|\.)")   # 归档 / 工具 / 全站公共 —— 不是站点


def _read_cap(p: Path, cap: int) -> str:
    """读文本文件前 cap 字符,不存在/读不动 → 空串(站点目录里零散文件多,别让任何一处抛)。"""
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:cap]
    except OSError:
        return ""


def _field(text: str, name: str, cap: int = 160) -> str:
    """从档案里取 '- 字段：值'(新格式)或 '- **字段**:值'(老格式)行。
    新统一结构的 00_档案.md 是「无加粗 + 全角冒号」格式,这里两种都认;占位值('(待探测)'/'无'/'—')当空。"""
    m = re.search(rf"^\s*-\s*(?:\*\*)?{name}(?:\*\*)?\s*[::：]\s*(.+)$", text, re.M)
    if not m:
        return ""
    val = m.group(1).replace("*", "").strip()[:cap]
    return "" if _PLACEHOLDER_RE.match(val) else val


def _md_section(text: str, title: str, cap: int = 200, lines: int = 1) -> str:
    """取 '## 一、当前结论' 这类小节的前几行(去 markdown 记号/占位行)拼成摘要。

    骨架报告里这些小节写的是占位/指针("→ 见详录最新一节"、"(后续探测在此增删节点)""(暂无记录)"),
    一律当空;真内容优先挑带「性质」的那条(总报告的定性惯用写法)。"""
    cands: list[str] = []
    grab = False
    for ln in text.splitlines():
        if ln.startswith("## "):
            if grab:
                break
            grab = title in ln
            continue
        if not grab:
            continue
        t = re.sub(r"^[-*\d.、\s]+", "", ln.replace("**", "")).strip()
        if not t or t.startswith(("→", "（暂无", "(暂无", "（待", "(待")):
            continue
        if "后续探测在此增删节点" in t or "见详录" in t or _PLACEHOLDER_RE.match(t):
            continue
        cands.append(t)
    pick = next((c for c in cands if "性质" in c), cands[0] if cands else "")
    out = " ".join([pick] + cands[:max(0, lines - 1)]) if pick else ""
    return out[:cap]


def _mm_meta(src_text: str) -> dict:
    """思维导图源文件元信息:节点数 / 分支数 / 是否还是占位骨架(统一结构每站 8 分支)。"""
    branches = re.findall(r"^##\s+(.+)$", src_text, re.M)
    body = _MM_PLACEHOLDER_RE.sub("", src_text)
    nodes = len(re.findall(r"^\s*-\s+\S", body, re.M))
    real = len(re.sub(r"\s", "", re.sub(r"^(#|---).*$", "", body, flags=re.M))) >= 200
    return {"nodes": nodes, "branches": len(branches), "real": real}


def _is_site_folder(p: Path) -> bool:
    """新统一结构(2026-10-07 结构整顿):报告/总报告.md = 站点标志(域名式/簇级文件夹都算);
    老站兜底:00_档案.md / 报告·证据·工作区 目录 / 域名式目录名。归档与工具目录一律不算。"""
    if _ARCHIVE_DIR_RE.match(p.name):
        return False
    if (p / "报告" / "总报告.md").is_file() or (p / "00_档案.md").is_file():
        return True
    if (p / "报告").is_dir() or (p / "证据").is_dir() or (p / "工作区").is_dir():
        return True
    return "." in p.name


def shezha_inventory(force: bool = False) -> dict:
    """站点总库:扫 01_目标档案 各库的站点文件夹,解析站点元数据(新统一结构优先)。

    数据源:
      ① 00_档案.md 的 状态/定性/实名线索/关联(存在就用;新格式无加粗全角冒号也认);
      ② 没有 00_档案 时从 报告/总报告.md 派生 —— 定性=「一、当前结论」首条、实名=「三、实名定位进展」首条,
         关联=思维导图源「基建与关联」分支;
      ③ 思维导图元信息(报告/思维导图源文件.md + 思维导图.html,标注是否还是占位骨架);
      ④ 站点根部的零散文件(旧证据/探针脚本/快照…,只登记不搬)。
    缓存 站点总库.json(1 小时新鲜度)。
    """
    st = shezha_state()
    if not st.get("configured"):
        return {"sites": [], "generated": None}
    cache = Path(st["workdir"]) / "站点总库.json"
    if not force and cache.exists():
        try:
            if time.time() - cache.stat().st_mtime < 3600:
                return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    arch = Path(st["root"]) / "01_目标档案"
    sites: list[dict] = []
    if arch.is_dir():
        for libdir in sorted(arch.iterdir()):
            if not libdir.is_dir() or libdir.name.startswith((".", "00_")):
                continue
            for f in sorted(libdir.iterdir()):
                if not f.is_dir() or not _is_site_folder(f):
                    continue
                text = _read_cap(f / "00_档案.md", 6000)
                rep_text = _read_cap(f / "报告" / "总报告.md", 12000)
                mm_text = _read_cap(f / "报告" / "思维导图源文件.md", 6000)
                status = _field(text, "状态") or ("停打" if "不予探测" in f.name or "别打" in f.name else "")
                profile = _field(text, "定性") or _md_section(rep_text, "当前结论", 160, 1)
                identity = _field(text, "实名线索", 100) or _md_section(rep_text, "实名定位进展", 100, 1)
                related = _field(text, "关联", 100) or _md_section(mm_text, "基建与关联", 100, 2)
                mindmaps = sorted(p.name for p in list(f.glob("思维导图*"))
                                  + list((f / "报告").glob("思维导图*")))
                mm = _mm_meta(mm_text) if mm_text else {"nodes": 0, "branches": 0, "real": False}
                mm["html"] = (f / "报告" / "思维导图.html").is_file()
                mm["old"] = (sorted(p.name for p in (f / "报告" / "导图_旧版").glob("*.html"))
                             if (f / "报告" / "导图_旧版").is_dir() else [])
                # 根部零散文件:标准件之外的东西(旧证据 jsonl / 探针脚本 / 首页快照…),只登记不搬
                loose = sorted(p.name for p in f.iterdir()
                               if p.is_file() and p.name not in ("00_档案.md", ".DS_Store"))
                hint_text = profile + " " + identity
                if _LEGIT_RE.search(hint_text) and not _RESULT_RE.search(hint_text):
                    hint = "legit"           # 定性里只有备案/资质类证据 → 疑似正规,待人工复核
                elif _RESULT_RE.search(hint_text):
                    hint = "result"          # 定性里就有战果词(shell/拖库/聊天记录/实名…)
                elif status in ("死", "停放", "死/停放"):
                    hint = "dead"            # 判死/停放
                else:
                    hint = "probe"           # 只有探测
                sites.append({
                    "domain": f.name.split("_")[0].lower() or f.name.lower(),
                    "name": f.name, "lib": libdir.name,
                    "status": status, "profile": profile, "identity": identity, "related": related,
                    "mindmaps": mindmaps, "mm": mm, "loose": loose[:40], "hint": hint,
                })
    data = {"generated": time.strftime("%F %T"), "sites": sites}
    try:
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return data


def shezha_cluster_view(force: bool = False) -> dict:
    """簇→站点两级视图:文件名自明簇(XX簇/体系/门族/舰队/农场/站群)+ AI 归簇缓存合并。"""
    inv = shezha_inventory(force=force)
    st = shezha_state()
    ai: dict = {}
    if st.get("configured"):
        try:
            ai = json.loads((Path(st["workdir"]) / "簇划分.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ai = {}
    assigned: dict[str, str] = {}
    for note, domains in (ai.get("ai_clusters") or {}).items():
        for d in domains:
            assigned[d] = note
    groups: dict[str, list[dict]] = {}
    fam_re = re.compile(r"(簇|体系|门族|舰队|农场|站群|家族)")
    for s in inv["sites"]:
        if s["domain"] in assigned:
            groups.setdefault(assigned[s["domain"]], []).append(s)
            continue
        note = s["name"].split("_", 1)[1] if "_" in s["name"] else ""
        if fam_re.search(note):
            groups.setdefault(note, []).append(s)
        else:
            groups.setdefault("未归类", []).append(s)
    reviews = shezha_reviews()
    archives = shezha_archives()
    n_archived = 0
    out = []
    for k, v in groups.items():
        agg = {"deep": 0, "mid": 0, "legit": 0, "surface": 0, "none": 0}
        for s in v:
            r = reviews.get(s["domain"])
            s["review"] = r
            s["archived"] = archives.get(s["domain"])   # {at, reason} 或 null
            if s["archived"]:
                n_archived += 1
                continue          # 归档 = 已结案:不进簇成果统计(树里也默认不显示)
            if r and r.get("degree") in agg:
                agg[r["degree"]] += 1
        v.sort(key=_degree_rank)             # 有成果在前(扎实 > 有获取 > 待审 > 探测/无材料)
        out.append({"name": k, "sites": v, "agg": agg})
    # 簇序:有成果的簇在前,其余按规模排(左栏自上而下"战果优先")
    out.sort(key=lambda c: (-(c["agg"]["deep"] + c["agg"]["mid"]),
                            -c["agg"]["deep"], -c["agg"]["mid"], -len(c["sites"]), c["name"]))
    return {"generated": inv.get("generated"), "clusters": out,
            "ai_done": bool(ai.get("ai_clusters")), "total": len(inv["sites"]),
            "archived": n_archived, "review_done": bool(reviews)}


_DEG_RANK = {"deep": 0, "mid": 1, "legit": 2, "surface": 3, "none": 4}


def _degree_rank(s: dict) -> float:
    """左栏排序键:审查级 > 机械信号兜底 > 未知。扎实 0 → 有获取 1 → 待人工复核 2 → 探测 3 → 无材料 4。"""
    r = s.get("review") or {}
    d = r.get("degree")
    if d in _DEG_RANK:
        return _DEG_RANK[d]
    return {"result": 1.5, "legit": 2.5}.get(s.get("hint"), 3.5)


def shezha_ai_cluster(rebuild: bool = False) -> str:
    """AI 归簇:把**尚未归簇**的站点交给模型分簇,结果**并入** 簇划分.json(默认增量,不覆盖已有簇)。
    rebuild=True 才全量重排(覆盖既有归簇、重烧一遍 token)。"""
    inv = shezha_inventory()
    cache = Path(shezha_state()["workdir"]) / "簇划分.json"
    try:
        prev = json.loads(cache.read_text(encoding="utf-8")).get("ai_clusters") or {}
    except (OSError, ValueError):
        prev = {}
    already = {d for v in prev.values() for d in (v or [])}
    fam_re = re.compile(r"(簇|体系|门族|舰队|农场|站群|家族)")
    rest = []
    for s in inv["sites"]:
        note = s["name"].split("_", 1)[1] if "_" in s["name"] else ""
        if fam_re.search(note):            # 文件名自明簇,不必喂模型
            continue
        if not rebuild and s["domain"] in already:
            continue                        # 增量模式:已归簇的跳过
        rest.append(s)
    if not rest:
        return f"无待归簇的新站点(已有 {len(already)} 站在 {len(prev)} 个 AI 簇里)"
    listing = "\n".join(f"{s['domain']} | {s['profile'][:70] or s['name']}" for s in rest[:600])
    prompt = (
        "下面是涉诈网站侦查项目的站点清单(域名 | 一句话定性)。请按**团伙/基建/业务簇**给它们归类"
        "(同模板站群、同底座、同收单/客服、同导航链、同资金通道的算一簇;孤立站点不硬凑)。\n"
        "只输出一个 JSON 对象,格式:\n"
        '{"ai_clusters": {"<簇名(短,可含底座/业务特征)>": ["域名", "..."]}}\n'
        "没有归进任何簇的域名不用输出(系统会自动落'未归类')。簇名用中文短语。\n\n" + listing)
    provider = _shezha_cfg().get("SHEZHA_PI_PROVIDER") or SHEZHA_DEF_PROVIDER
    model = _shezha_cfg().get("SHEZHA_PI_MODEL") or SHEZHA_DEF_MODEL
    try:
        r = subprocess.run(
            ["pi", "-p", "--provider", provider, "--model", model, "--no-session",
             "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files", prompt],
            capture_output=True, text=True, timeout=300)
    except Exception as e:
        return f"AI 归簇调用失败: {e}"
    m = re.search(r"\{.*\}", r.stdout or "", re.S)
    if not m:
        return f"AI 归簇失败(模型没给 JSON):{(r.stderr or '')[:200]}"
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return f"AI 归簇失败(JSON 解析): {e}"
    new = data.get("ai_clusters") or {}
    known = {s["domain"] for s in inv["sites"]}
    new = {k: [d for d in v if d in known] for k, v in new.items()
           if isinstance(v, list) and v}
    merged: dict[str, list[str]] = ({} if rebuild
                                     else {k: list(v or []) for k, v in prev.items()})
    for k, v in new.items():
        cur = merged.setdefault(k, [])
        for d in v:
            if d not in cur:
                cur.append(d)
    cache.write_text(json.dumps({"ai_clusters": merged,
                                 "generated": time.strftime("%F %T")},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    n = sum(len(v) for v in new.values())
    return (f"AI 归簇完成:本次 {len(rest)} 站待归簇,新收编 {n} 站;"
            f"现共 {len(merged)} 簇 / {sum(len(v) for v in merged.values())} 站"
            f"({'全量重排' if rebuild else '增量并入,未覆盖旧簇'})")


def shezha_reviews() -> dict:
    """成果评审缓存:{domain: {degree: deep|mid|legit|surface|none, got/gap, by, rev, decision?}}。"""
    st = shezha_state()
    if not st.get("configured"):
        return {}
    try:
        d = json.loads((Path(st["workdir"]) / "成果评审.json").read_text(encoding="utf-8"))
        return d.get("reviews") or {}
    except (OSError, ValueError):
        return {}


def shezha_review_ai(retry_fallback: bool = True) -> str:
    """AI 全库读报告·评成果程度(tools/review_results.py 后台跑,增量落盘 成果评审.json)。
    retry_fallback=True = 手动点,连内容安全过滤拒过的站一起重读;
    False = 自动同步走这条,只读新站/材料变过的站(省配额)。"""
    st = shezha_state()
    if not st.get("configured"):
        return "先绑定作战仓库根目录"
    ws = Path(st["workdir"])
    if not (ws / "站点总库.json").exists():
        return "站点总库还没建 —— 点一次「AI 归簇」或等 1 小时缓存自动建"
    provider = _shezha_cfg().get("SHEZHA_PI_PROVIDER") or SHEZHA_DEF_PROVIDER
    model = _shezha_cfg().get("SHEZHA_PI_MODEL") or SHEZHA_DEF_MODEL
    cmd = [sys.executable, str(ROOT / "tools" / "review_results.py"),
           "--ws", str(ws), "--provider", provider, "--model", model]
    if retry_fallback:
        cmd.append("--retry-fallback")   # 手动点 = 有意全量重读,连内容过滤拒过的站一起重来
    return _background_job(ws, "review", cmd, timeout=3600)


def shezha_review_decide(domain: str, decision: str) -> str:
    """人工复核落槌(「待人工审核」档专属):
    stop=记入停打名单(下批不再派发) / go=核实过继续打 / undo=撤销复核回到待审。
    判定落两处:① config.json stop_set(派发侧唯一真源) ② 成果评审.json 的 decision(界面回溯用)。
    人话优先:重读材料不会翻掉已落槌的决定(见 review_results.py 的 decision 保留)。"""
    st = shezha_state()
    if not st.get("configured"):
        return "✘ 先绑定作战仓库根目录"
    ws = Path(st["workdir"])
    domain = (domain or "").strip().lower()
    decision = (decision or "").strip().lower()
    if decision not in ("stop", "go", "undo"):
        return "✘ 未知复核动作"
    rv_path = ws / "成果评审.json"
    data = read_json(rv_path, {}) or {}
    reviews = data.get("reviews") or {}
    r = reviews.get(domain)
    if not r:
        return f"✘ {domain} 还没有成果判定,先跑「AI 审成果」"
    if r.get("degree") != "legit":
        return f"✘ {domain} 当前是 {r.get('degree')} 档,不是「待人工审核」"
    cfg_path = ws / "config.json"
    if decision in ("stop", "undo"):          # stop 增、undo 撤 —— 都动停打名单,没变化就不重写文件
        cfg = read_json(cfg_path, {}) or {}
        stop = [d for d in (cfg.get("stop_set") or []) if d != domain]
        if decision == "stop":
            stop.append(domain)
        if stop != (cfg.get("stop_set") or []):
            cfg["stop_set"] = stop
            try:
                cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as e:
                return f"✘ 写 config.json 失败:{e}"
    if decision == "stop":
        r["decision"], r["decided"] = "stop", time.strftime("%F %T")
        msg = f"{domain} 已记入停打名单(config.json stop_set):切批次/下批生成时不再派发"
    elif decision == "go":
        r["decision"], r["decided"] = "go", time.strftime("%F %T")
        msg = f"{domain} 已复核:继续打(只记录,不影响派发)"
    else:
        r.pop("decision", None)
        r.pop("decided", None)
        msg = f"{domain} 已撤销复核,回到「待人工审核」(停打名单里的记录也一并撤掉)"
    reviews[domain] = r
    data["reviews"] = reviews
    data.setdefault("generated", time.strftime("%F %T"))
    try:
        rv_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        return f"✘ 写 成果评审.json 失败:{e}"
    return msg


# ============ 归档结案:站点"查完了 / 不是涉诈" → 退出工作视图,也不再派发 ============
# 和「停手(decision=stop)」的分工,别混:
#   · 停手 = 打的过程中撞见疑似正规资产先停 → 只是"别继续碰它";
#   · 归档 = 人**已经有结论了**(不是涉诈 / 已交付收工)→ 从簇树工作视图里收走,
#            派发侧(excel_targets.py)也不再生成新目标;材料/评审/导图原地不动,随时可撤销。
# 真源只有一处:config.json 的 archive —— 键=域名,值={at, reason}。界面与派发侧都读它,
# 和 stop_set 一样是"人写的名单",AI 重读材料不会动它。


def shezha_archives() -> dict:
    """归档登记表 {域名: {at, reason}};没配置/没归档过就是 {}。"""
    st = shezha_state()
    if not st.get("configured"):
        return {}
    cfg = read_json(Path(st["workdir"]) / "config.json", {}) or {}
    arc = cfg.get("archive")
    return arc if isinstance(arc, dict) else {}


def _sz_archive_norm(domains) -> list[str]:
    """域名归一(小写去空)+ 去重保序。"""
    out: list[str] = []
    seen: set[str] = set()
    for d in domains:
        k = str(d or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _sz_archive_write(arc: dict) -> str:
    """归档表写回 config.json(与 review_decide 写 stop_set 同一条路:读改写)。"""
    st = shezha_state()
    if not st.get("configured"):
        return "✘ 先绑定作战仓库根目录"
    path = Path(st["workdir"]) / "config.json"
    cfg = read_json(path, {}) or {}
    if arc:
        cfg["archive"] = arc
    else:
        cfg.pop("archive", None)       # 表空了就把键去掉,config.json 不留空壳
    try:
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        return f"✘ 写 config.json 失败:{e}"
    return ""


def shezha_archive_add(domains, reason: str = "") -> str:
    """归档一批域名(已归档的不重复记,但报出数量)。"""
    doms = _sz_archive_norm(domains)
    if not doms:
        return "✘ 没有可归档的站点"
    if not shezha_state().get("configured"):
        return "✘ 先绑定作战仓库根目录"
    arc = dict(shezha_archives())
    at = time.strftime("%F %T")
    reason = (reason or "").strip()[:80]
    added = [d for d in doms if d not in arc]
    for d in added:
        arc[d] = {"at": at, "reason": reason}
    if added:
        err = _sz_archive_write(arc)
        if err:
            return err
    dup = f"(另有 {len(doms) - len(added)} 站本就在归档里)" if len(added) < len(doms) else ""
    return f"已归档 {len(added)} 站{dup}:退出工作视图,派发侧也不再生成新目标(共 {len(arc)} 站在归档里)"


def shezha_unarchive(domain: str) -> str:
    """撤销归档:回到工作视图(不回派发名单 —— 要重打还得 --redo 或在批次里点名)。"""
    doms = _sz_archive_norm([domain])
    if not doms:
        return "✘ 域名不能为空"
    d = doms[0]
    arc = dict(shezha_archives())
    if d not in arc:
        return f"✘ {d} 不在归档里"
    arc.pop(d, None)
    err = _sz_archive_write(arc)
    if err:
        return err
    return f"{d} 已撤销归档,回到工作视图(还有 {len(arc)} 站在归档里)"


def shezha_archive_cluster(name: str) -> str:
    """整簇归档:一个簇整个查完了/不是涉诈 → 一次把簇内还没归档的站收走。"""
    name = (name or "").strip()
    if not name:
        return "✘ 簇名不能为空"
    c = next((x for x in (shezha_cluster_view().get("clusters") or [])
              if x.get("name") == name), None)
    if not c:
        return f"✘ 没找到簇「{name}」"
    sites = c.get("sites") or []
    doms = [s["domain"] for s in sites if not s.get("archived")]
    if not doms:
        return f"✘ 「{name}」里的 {len(sites)} 站都已归档"
    msg = shezha_archive_add(doms, f"整簇归档:{name}")
    return msg if msg.startswith("✘") else f"簇「{name}」{msg}"


def shezha_archive_pending() -> str:
    """一键归档全部「待人工审核」且没落槌的站 —— 复核结论:不是涉诈,结案。
    与「续打」相反:这批人的判断是"站点正规,不必再打",归档即可退出工作视图与派发。"""
    reviews = shezha_reviews()
    doms: list[str] = []
    for c in shezha_cluster_view().get("clusters") or []:
        for s in c.get("sites") or []:
            r = reviews.get(s["domain"]) or {}
            if s.get("archived") or r.get("decision"):
                continue
            deg = r.get("degree")
            legit = (deg == "legit") if deg else (s.get("hint") == "legit")
            if legit:
                doms.append(s["domain"])
    if not doms:
        return "✘ 没有「待人工审核」的站可归档"
    return shezha_archive_add(doms, "复核:不是涉诈,归档结案")


# ============ 自动同步:作战仓库落了新料 → 自动跟上总库/归簇/评审 ============
# 设计要点:
#   · 变更检测走"指纹"——扫 01_目标档案 站点目录 mtime + 02_批次报告 + 总表/登记表,
#     只 stat 不读内容,几百站一次几十毫秒;指纹没变就什么都不做(不烧 token)。
#   · 增量:归簇只喂未归簇站、评审只读未评/材料变过的站(见 review_results.py 的 sig)。
#   · 节流:两次 AI 动作之间至少隔 SZ_SYNC_MIN_GAP 秒;命中 429 限流则退避到重置之后。
#   · 开关:SHEZHA_AUTOSYNC=0 关掉;状态落 logs/autosync_status.json,前端可看可点。

SZ_SYNC_TICK = 180          # 扫描间隔(秒)
SZ_SYNC_MIN_GAP = 1800      # 两次 AI 动作最小间隔(秒)
SZ_SYNC_BACKOFF = 3600      # 限流后退避(秒)
_szs_lock = threading.Lock()


def _sz_sync_state(ws: Path) -> dict:
    return read_json(ws / "logs" / "autosync_status.json", {}) or {}


def _sz_repo_fingerprint(root: Path) -> str:
    """作战仓库内容指纹:站点目录 mtime + 批次报告/总表/登记表 mtime,拼起来 sha1。
    只 stat 不读文件,几百站也就几十毫秒。新建/改动站点或落了新批次报告 → 指纹变。"""
    h = hashlib.sha1()
    arch = root / "01_目标档案"
    n = 0
    if arch.is_dir():
        for lib in sorted(p for p in arch.iterdir() if p.is_dir() and not p.name.startswith(".")):
            for site in sorted(p for p in lib.iterdir() if p.is_dir()):
                try:
                    h.update(f"{lib.name}/{site.name}:{int(site.stat().st_mtime)}".encode())
                except OSError:
                    continue
                n += 1
    for sub in ("02_批次报告", "00_总控"):
        d = root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob("**/*.md"))[-400:]:
            try:
                h.update(f"{sub}/{p.name}:{int(p.stat().st_mtime)}".encode())
            except OSError:
                pass
    h.update(str(n).encode())
    return h.hexdigest()[:16]


def _sz_pending(ws: Path) -> dict:
    """当前有多少活等着做(全部零 AI 调用)。口径与 review_results.py 默认行为一致:
    新站 + 材料变过的已读站 算待办;fallback(内容过滤拒过)与待补指纹的站 不算。"""
    root = ws.parent.parent
    inv = read_json(ws / "站点总库.json", {}) or {}
    sites = inv.get("sites") or []
    rv = (read_json(ws / "成果评审.json", {}) or {}).get("reviews") or {}
    cl = (read_json(ws / "簇划分.json", {}) or {}).get("ai_clusters") or {}
    assigned = {d for v in cl.values() for d in (v or [])}
    fam_re = re.compile(r"(簇|体系|门族|舰队|农场|站群|家族)")
    unclustered = 0
    for s in sites:
        note = s["name"].split("_", 1)[1] if "_" in s["name"] else ""
        if not fam_re.search(note) and s["domain"] not in assigned:
            unclustered += 1
    # 评审待办按**域名**归并(同域多夹合并),口径与 review_results.py 一致
    by_dom: dict[str, list[dict]] = {}
    for s in sites:
        by_dom.setdefault(s["domain"], []).append(s)
    new_sites = changed = fallback = resign = stale = 0
    for dom, group in by_dom.items():
        prev = rv.get(dom) or {}
        by = prev.get("by")
        if by is None:
            new_sites += 1
        elif by == "ai":
            if prev.get("rev") != _rv_rev:
                # 口径升级(与 review_results.py 一致):deep/mid/legit 重读;surface/none 盖章放行
                if prev.get("degree") in ("deep", "mid", "legit"):
                    stale += 1
                elif not prev.get("sig"):
                    resign += 1
            elif not prev.get("sig"):
                resign += 1                       # 补盖指纹,免费
            elif _rv_material_sig is not None:
                cur = "|".join(_rv_material_sig(ws, s) for s in group)
                if cur and cur != prev["sig"]:
                    changed += 1                 # 材料更新过 → 要重读
        elif by == "fallback":
            fallback += 1
    return {"sites": len(sites), "domains": len(by_dom),
            "new_sites": new_sites, "changed": changed, "stale": stale,
            "fallback": fallback, "resign": resign,
            "todo_review": new_sites + changed + stale, "unclustered": unclustered}


def _sz_rate_limited(ws: Path) -> bool:
    """上一轮评审是否死于 429 限流(GLM 5h 限额 / 平台限流)。是则本轮跳过 AI。"""
    out = ((read_json(ws / "logs" / "review_status.json", {}) or {}).get("output") or "")
    tail = out[-1500:]
    return ("rate_limit" in tail or "1308" in tail or "429" in tail
            or "使用上限" in tail or "限额" in tail)


def shezha_autosync_tick(force: bool = False) -> dict:
    """跑一轮自动同步。force=True = 忽略节流(前端「立即同步」用)。"""
    st = shezha_state()
    if not st.get("configured"):
        return {"ran": False, "why": "未绑定作战仓库"}
    root = Path(st["root"]); ws = Path(st["workdir"])
    now = time.time()
    s = _sz_sync_state(ws)
    fp = _sz_repo_fingerprint(root)
    pend = _sz_pending(ws)
    changed = fp != s.get("fingerprint")
    base = {"fingerprint": fp, "pending": pend, "changed": changed,
            "last_tick": time.strftime("%F %T")}
    if not changed and not force:
        base.update(ran=False, why="仓库无变化", **base)
        s.update(base); _sz_write_sync(ws, s)
        return base
    with _szs_lock:
        last_ai = float(s.get("last_ai_ts") or 0)
        if not force and now - last_ai < SZ_SYNC_MIN_GAP:
            why = f"距上次 AI 动作不足 {SZ_SYNC_MIN_GAP}s(节流中)"
            s.update(base, ran=False, why=why); _sz_write_sync(ws, s)
            return {**base, "ran": False, "why": why}
        if not force and _sz_rate_limited(ws):
            s["backoff_until"] = now + SZ_SYNC_BACKOFF
            s.update(base, ran=False, why="上一轮命中限流,退避中")
            _sz_write_sync(ws, s)
            return {**base, "ran": False, "why": "限流退避中"}
        actions = []
        # 1) 总库永远先重建(零 AI,保证新站点立刻出现在树上)
        try:
            shezha_inventory(force=True)
            actions.append("总库重建")
        except Exception as e:  # noqa: BLE001
            actions.append(f"总库重建失败({e})")
        # 2) 有未归簇站 → 增量归簇(单次模型调用)
        pend = _sz_pending(ws)
        if pend["unclustered"] and not _sz_job_running(ws, "review"):
            try:
                msg = shezha_ai_cluster(rebuild=False)
                actions.append(f"归簇:{msg[:60]}")
            except Exception as e:  # noqa: BLE001
                actions.append(f"归簇失败({e})")
        # 3) 有新站/材料变过的站 → 增量评审(后台;默认不重试内容过滤拒过的站)
        pend = _sz_pending(ws)
        if pend["todo_review"] and not _sz_job_running(ws, "review"):
            r = shezha_review_ai(retry_fallback=False)
            actions.append(f"评审:{r[:50]}")
        s.update(base, ran=True, why="有变化,已同步",
                 actions=actions, last_ai_ts=now, last_change=time.strftime("%F %T"))
        _sz_write_sync(ws, s)
        return s


def _sz_job_running(ws: Path, name: str) -> bool:
    st = read_json(ws / "logs" / f"{name}_status.json", {}) or {}
    return bool(st.get("running"))


def _sz_write_sync(ws: Path, s: dict) -> None:
    try:
        (ws / "logs").mkdir(parents=True, exist_ok=True)
        (ws / "logs" / "autosync_status.json").write_text(
            json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _sz_autosync_loop() -> None:
    """后台线程:每 SZ_SYNC_TICK 秒扫一次仓库指纹,变了就同步。dashboard 重启后自动复活。"""
    while True:
        try:
            cfg_on = str(_shezha_cfg().get("SHEZHA_AUTOSYNC", "1")).strip() not in ("0", "off", "false")
            if cfg_on:
                shezha_autosync_tick(force=False)
        except Exception:  # noqa: BLE001  绝不让同步线程把 dashboard 带崩
            pass
        time.sleep(SZ_SYNC_TICK)


def _start_autosync() -> None:
    threading.Thread(target=_sz_autosync_loop, daemon=True).start()


_CN_ORG_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{3,24}(?:公司|工作室|集团|事务所|中心|平台|科技|网络)")


def _cluster_edges(sites: list[dict]) -> list[dict]:
    """机械关系边:同主域 / 档案互指 / 实名同源(从既有档案字段算,零成本)。"""
    edges: list[dict] = []
    doms = [s["domain"] for s in sites]
    domset = set(doms)

    def base_dom(d: str) -> str:
        parts = d.split(".")
        n = 3 if d.endswith((".com.cn", ".net.cn", ".org.cn", ".gov.cn")) else 2
        return ".".join(parts[-n:])

    by_base: dict[str, list[str]] = {}
    for d in doms:
        by_base.setdefault(base_dom(d), []).append(d)
    for b, ds in by_base.items():
        for i in range(len(ds) - 1):
            edges.append({"a": ds[i], "b": ds[i + 1], "type": "同主域", "why": b})
    for s in sites:
        rel = s.get("related") or ""
        for d in domset:
            if d != s["domain"] and len(d) > 5 and d in rel:
                edges.append({"a": s["domain"], "b": d, "type": "档案互指", "why": f"{s['name'][:24]} 关联字段"})
    tok_map: dict[str, list[str]] = {}
    for s in sites:
        toks = set(_CN_ORG_RE.findall(s.get("identity") or "")) | set(_CN_ORG_RE.findall(s.get("profile") or ""))
        for tk in toks:
            tok_map.setdefault(tk[:20], []).append(s["domain"])
    for tk, ds in tok_map.items():
        if 1 < len(ds) <= 6:
            for i in range(len(ds) - 1):
                edges.append({"a": ds[i], "b": ds[i + 1], "type": "实名同源", "why": tk})
    ded: dict = {}
    for e in edges:
        key = (min(e["a"], e["b"]), max(e["a"], e["b"]), e["type"])
        ded[key] = e
    return list(ded.values())


def shezha_cluster_detail(name: str) -> dict | None:
    """簇详情:站点 + 机械关系边 + AI 补充(若有缓存)。"""
    cv = shezha_cluster_view()
    cl = next((c for c in cv["clusters"] if c["name"] == name), None)
    if not cl:
        return None
    st = shezha_state()
    ai = None
    if st.get("configured"):
        cache = read_json(Path(st["workdir"]) / "簇关系.json", {})
        ai = (cache.get("clusters") or {}).get(name)
    return {"name": name, "sites": cl["sites"], "agg": cl.get("agg"),
            "edges": _cluster_edges(cl["sites"]), "ai": ai}


def shezha_cluster_ai(name: str) -> str:
    """AI 补全簇关系:通读簇内各站的定性/实名/关联/成果,产出综述 + 关系边(缓存 簇关系.json)。"""
    cv = shezha_cluster_view()
    cl = next((c for c in cv["clusters"] if c["name"] == name), None)
    if not cl:
        return f"没有这个簇: {name}"
    sites = cl["sites"]
    if len(sites) < 2:
        return "这个簇只有 1 个站,没有关系可分析"
    listing = "\n".join(
        f"- {s['domain']} | 状态:{s['status'] or '?'} | 定性:{s['profile'][:60] or '(空)'} | "
        f"实名:{s['identity'][:40] or '无'} | 关联:{s['related'][:40] or '无'} | "
        f"成果:{(s.get('review') or {}).get('got', '')[:36] or '无'}"
        for s in sites[:60])
    prompt = (
        f"你在分析涉诈侦查项目里「{name}」这个簇(按团伙/基建/业务归的簇)。下面是簇内 {len(sites)} 个站点的档案要点。\n"
        "请通读并输出 JSON(不要解释):\n"
        '{"summary": "3~5 句综述:这个簇是什么团伙/业务、站点间如何分工(主站/落地/资源/收单)、最硬的战果",\n'
        ' "edges": [{"a": "域名", "b": "域名", "type": "同法人|同收款|同底座|同收单|跳转引流|同批资产|其它", "why": "≤16字依据"}]}\n'
        "edges 只写材料里有依据的关系(域名必须用清单里的);没有就少写,不许编。\n\n站点清单:\n" + listing)
    provider = _shezha_cfg().get("SHEZHA_PI_PROVIDER") or SHEZHA_DEF_PROVIDER
    model = _shezha_cfg().get("SHEZHA_PI_MODEL") or SHEZHA_DEF_MODEL
    r = subprocess.run(
        ["pi", "-p", "--provider", provider, "--model", model, "--no-session",
         "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files", prompt],
        capture_output=True, text=True, timeout=300)
    m = re.search(r"\{.*\}", r.stdout or "", re.S)
    if not m:
        return f"AI 没给 JSON:{(r.stderr or r.stdout or '')[:160]}"
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return f"JSON 解析失败: {e}"
    known = {s["domain"] for s in sites}
    edges = [e for e in (data.get("edges") or [])
             if isinstance(e, dict) and e.get("a") in known and e.get("b") in known and e.get("a") != e.get("b")]
    entry = {"summary": str(data.get("summary") or "")[:600],
             "edges": edges, "generated": time.strftime("%F %T")}
    st2 = shezha_state()
    cache_path = Path(st2["workdir"]) / "簇关系.json"
    cache = read_json(cache_path, {})
    cache.setdefault("clusters", {})[name] = entry
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return f"簇关系 AI 分析完成:{name}(补 {len(edges)} 条关系边)"


def shezha_site_view(domain: str) -> dict | None:
    """任意站点(不限本批)的档案视图:总库条目 + 档案文件 + 常驻 + 本批实况。"""
    inv = shezha_inventory()
    s = next((x for x in inv["sites"] if x["domain"] == domain.lower()), None)
    if not s:
        return None
    arch = Path(shezha_state()["root"]) / "01_目标档案" / s["lib"] / s["name"]

    def _read(p: Path, cap: int = 12000) -> str:
        try:
            return p.read_text(encoding="utf-8", errors="replace")[:cap]
        except OSError:
            return ""

    def _list(d: Path) -> list[str]:
        try:
            return sorted(x.name for x in d.iterdir() if x.is_file())
        except OSError:
            return []

    st = shezha_state()
    tj = {t.get("id") for t in (read_json(Path(st["workdir"]) / "targets.json", {}).get("targets") or [])}
    board = read_json(Path(st["workdir"]) / "board.json", {}).get("questions") or {}
    entry = board.get(s["domain"]) or {}
    # 新统一结构:报告/总报告.md(唯一完整报告)+ 时间线.md + 思维导图源文件.md/.html;
    # 00_档案.md 若在也带上;老布局(REPORT.md/根目录报告 md)兜底拼进 profile_md。
    prof = _read(arch / "00_档案.md")
    extra = []
    rep_md = _read(arch / "报告" / "总报告.md", 60000)
    tl_md = _read(arch / "报告" / "时间线.md", 20000)
    mm_md = _read(arch / "报告" / "思维导图源文件.md", 20000)
    if not rep_md:
        rep_md = _read(arch / "REPORT.md", 6000)
    if not prof and not rep_md:
        prof = _read(arch / "REPORT.md", 6000)
    root_mds = [x for x in sorted(arch.glob("*.md"), key=lambda x: x.stat().st_mtime)
                if x.name not in ("00_档案.md", "REPORT.md")][-2:]
    for pp in root_mds:
        extra.append(f"### {pp.name}\n" + _read(pp, 2000))
    if extra:
        prof = (prof + "\n\n" if prof else "") + "\n\n".join(extra)
    # 目录清点:证据/交付/大件存证 + 根部零散文件(哨位运行件单独归组,按规范它们永不搬)
    stakeout = [n for n in (s.get("loose") or []) if re.match(r"(stakeout|watch|probe).*\.(py|log|json|state)", n, re.I)]
    loose_other = [n for n in (s.get("loose") or []) if n not in stakeout]
    return {**s, "archive": str(arch),
            "profile_md": prof,
            "has_archive_md": (arch / "00_档案.md").is_file(),
            "report_md": rep_md,
            "report_truncated": (arch / "报告" / "总报告.md").is_file()
                                and (arch / "报告" / "总报告.md").stat().st_size > 60000,
            "timeline_md": tl_md,
            "mm_md": mm_md,
            "mm_html": (arch / "报告" / "思维导图.html").is_file(),
            "mm_old": (sorted(p.name for p in (arch / "报告" / "导图_旧版").glob("*"))
                       if (arch / "报告" / "导图_旧版").is_dir() else []),
            "findings_md": _read(arch / "工作区" / "FINDINGS.md", 8000) or _read(arch / "FINDINGS.md", 8000),
            "reports": _list(arch / "报告"), "evidence": _list(arch / "证据")[-30:],
            "evidence_n": len(_list(arch / "证据")),
            "deliver_n": len(_list(arch / "交付")),
            "big_n": len(_list(arch / "大件存证")),
            "stakeout": stakeout, "loose_other": loose_other,
            "residents": shezha_residents(s["domain"])["rows"],
            "in_batch": s["domain"] in tj,
            "review": shezha_reviews().get(s["domain"]),
            "archived": shezha_archives().get(s["domain"]),   # {at, reason} 或 null(注意与 archive=目录路径 不同)
            "live": ({"status": entry.get("status"), "attempts": entry.get("attempts"),
                      "findings": entry.get("findings_count"),
                      "delivered": entry.get("delivered")} if entry else None)}


def shezha_mindmap_path(domain: str) -> Path | None:
    """站点导图 html 的***真实路径***(给只读路由用):域名→总库条目→报告/思维导图.html。
    路径由总库条目拼出(不接受调用方给路径),并断言落在站点档案目录内 —— 防目录穿越。"""
    st = shezha_state()
    if not st.get("configured"):
        return None
    inv = shezha_inventory()
    s = next((x for x in inv["sites"] if x["domain"] == (domain or "").lower()), None)
    if not s:
        return None
    arch = (Path(st["root"]) / "01_目标档案" / s["lib"] / s["name"]).resolve()
    target = (arch / "报告" / "思维导图.html").resolve()
    if arch not in target.parents or not target.is_file():
        return None
    return target



# 独立导图 html 里的 markmap 装载器是 CDN 地址 —— 现场不保证能出网,直接打开就是白屏。
# 这里把它换成本地 assets/markmap/(d3 + markmap-lib + markmap-view + 本地装载器),
# 渲染逻辑与官方 autoloader 一致,只是不走网络。
_MINDMAP_CDN_RE = re.compile(
    r'<script[^>]+src=["\']https?://[^"\']*markmap-autoloader[^"\']*["\'][^>]*>\s*</script>')
_MINDMAP_LOCAL_TAGS = (
    '<script src="/assets/markmap/d3.js"></script>\n'
    '<script src="/assets/markmap/markmap-lib.iife.js"></script>\n'
    '<script src="/assets/markmap/markmap-view.js"></script>\n'
    '<script src="/assets/markmap/autoloader-local.js"></script>')


# 生成脚本里那句 `.markmap-div{height:100vh}` 跟它自己产出的标记(`<div class="markmap">`)
# 对不上,于是容器高度是 auto —— 直接打开这些 html,导图只在顶部一条 ~150px 的缝里。
# 这里补一条真正生效的高度规则,让导图铺满窗口(iframe 里就是铺满 iframe)。
_MINDMAP_SIZE_CSS = (
    "<style>html,body{height:100%;margin:0}"
    ".markmap{width:100%;height:100vh;min-height:360px;position:relative}"
    ".markmap-local-tools{position:absolute;right:12px;top:12px;z-index:9;display:flex;gap:6px}"
    ".markmap-local-tools button{font:12px/1 -apple-system,\"PingFang SC\",sans-serif;padding:6px 10px;"
    "border:1px solid rgba(148,163,184,.45);border-radius:6px;background:rgba(15,23,42,.72);color:#e2e8f0;cursor:pointer}"
    ".markmap-local-tools button:hover{background:rgba(15,23,42,.9)}"
    "</style>")


def _mindmap_localize(html: str) -> str:
    """把独立导图页的 markmap CDN 引用换成本地资源,并补上容器高度;顺带打标记免得二次替换。"""
    if "markmap-localized" in html:
        return html
    out, n = _MINDMAP_CDN_RE.subn(_MINDMAP_LOCAL_TAGS, html)
    if not n and "</head>" in html:
        # 没匹配到 CDN 标签(格式有变):退而在 </head> 前补本地标签,别让页面白屏
        out = html.replace("</head>", _MINDMAP_LOCAL_TAGS + "\n</head>", 1)
    if "</head>" in out:
        out = out.replace("</head>", _MINDMAP_SIZE_CSS + "<!--markmap-localized-->\n</head>", 1)
    return out

def shezha_timeline(days: int = 7) -> list[dict]:
    """探测时间线:findings + 批次汇总 + 开车道 + 巡检/周报,按时间倒序(零 LLM,直接从文件攒)。"""
    st = shezha_state()
    if not st.get("configured"):
        return []
    root, ws = Path(st["root"]), Path(st["workdir"])
    since = time.time() - max(1, days) * 86400
    events: list[dict] = []

    flog = ws / "logs" / "findings.jsonl"
    if flog.exists():
        for line in flog.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = str(e.get("ts") or "")
            try:
                t = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
            if t < since:
                continue
            title = str(e.get("title") or "")
            kind = ("data" if "数据战果" in title else
                    "identity" if "实名" in title else
                    "deliver" if e.get("event") == "delivered" else "finding")
            events.append({"ts": ts, "kind": kind, "site": e.get("target_id") or "",
                           "text": title or str(e.get("summary") or "")[:80]})
    rep_dir = root / "02_批次报告"
    if rep_dir.is_dir():
        for p in rep_dir.rglob("*.md"):
            m = re.match(r"本批汇总_(\d{8})_批次([\d一二三四五六七八九十]+)_(.+)\.md", p.name)
            if m:
                try:
                    t = time.mktime(time.strptime(m.group(1), "%Y%m%d"))
                except ValueError:
                    continue
                if t >= since:
                    events.append({"ts": time.strftime("%Y-%m-%d 12:00", time.localtime(t)),
                                   "kind": "batch", "site": f"批次{m.group(2)}",
                                   "text": m.group(3)[:80], "path": str(p)})
            elif p.name.startswith("周报_") and p.stat().st_mtime >= since:
                events.append({"ts": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
                               "kind": "weekly", "site": "", "text": p.name[3:-3], "path": str(p)})
    for p in root.glob("LANE_CLAIM_批次*.md"):
        if p.stat().st_mtime >= since:
            events.append({"ts": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
                           "kind": "lane", "site": p.stem.replace("LANE_CLAIM_", ""),
                           "text": "开车道(自动批次开工)"})
    patrol_dir = ws / "logs" / "巡检"
    if patrol_dir.is_dir():
        for p in sorted(patrol_dir.glob("巡检_*.md")):
            if p.stat().st_mtime >= since:
                events.append({"ts": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
                               "kind": "patrol", "site": "", "text": f"总Agent巡检:{p.stem[3:]}", "path": str(p)})
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:400]


def _background_job(ws: Path, name: str, cmd: list[str], timeout: int = 900) -> str:
    """通用后台作业(周报/巡检等):status 落 logs/<name>_status.json,防重入。"""
    status_path = ws / "logs" / f"{name}_status.json"
    with _close_lock:
        prev = read_json(status_path, {}) or {}
        if prev.get("running"):
            # 上一次作业可能已随 dashboard 重启死亡(线程没了,状态却永远"运行中"):
            # 超过 timeout 还没动静 = 陈旧,允许重开。
            stale = True
            try:
                started = time.mktime(time.strptime(prev.get("started") or "", "%Y-%m-%d %H:%M:%S"))
                stale = (time.time() - started) > (timeout + 60)
            except (ValueError, TypeError):
                pass
            if not stale:
                return f"上一次{name}还在进行中"
    (ws / "logs").mkdir(parents=True, exist_ok=True)

    def _job() -> None:
        status = {"running": True, "started": time.strftime("%F %T")}
        status_path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            status.update(rc=r.returncode, running=False,
                          finished=time.strftime("%F %T"),
                          output=((r.stdout or "") + (r.stderr or ""))[-4000:])
        except Exception as e:  # noqa: BLE001
            status.update(rc=-1, running=False, output=repr(e))
        status_path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")

    threading.Thread(target=_job, daemon=True).start()
    return f"{name}已开始,状态区几秒内刷新"


def shezha_weekly() -> str:
    """AI 周报:把近 14 天时间线交给模型写周报,落 02_批次报告/YYYY-MM/周报_YYYYMMDD.md。"""
    st = shezha_state()
    if not st.get("configured"):
        return "先绑定作战仓库根目录"
    ws = Path(st["workdir"])
    tl = shezha_timeline(days=14)
    if not tl:
        return "近 14 天没有可写的时间线事件"
    today = datetime.date.today()
    body = "\n".join(f"- [{e['ts']}] ({e['kind']}) {e['site']} {e['text']}" for e in tl)
    out_dir = Path(st["root"]) / "02_批次报告" / today.strftime("%Y-%m")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"周报_{today.strftime('%Y%m%d')}.md"
    prompt = (f"你是涉诈网站侦查项目的周报书记员。下面是近 14 天的探测时间线(系统自动攒的)。\n"
              f"写一份周报 markdown:以 '# 周报({today} 前后14天)' 开头;分节:①批次与车道 "
              "②数据面战果(聊天记录/库表/PII) ③实名定位进展 ④常驻与盯守变动 "
              "⑤值得人工注意的(僵尸常驻/正规资产/出口被封) ⑥下周建议。\n"
              "只用时间线里的事实,没有的小节写\"(本周无)\";不得编造。\n\n# 时间线\n" + body[:40000])
    provider = _shezha_cfg().get("SHEZHA_PI_PROVIDER") or SHEZHA_DEF_PROVIDER
    model = _shezha_cfg().get("SHEZHA_PI_MODEL") or SHEZHA_DEF_MODEL
    cmd = ["pi", "-p", "--provider", provider, "--model", model, "--no-session",
           "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files", prompt]
    py = sys.executable
    wrapper = [
        py, "-c",
        "import subprocess,sys,pathlib\n"
        "r = subprocess.run(sys.argv[1:-1], capture_output=True, text=True, timeout=800)\n"
        "body = (r.stdout or '').strip()\n"
        "if r.returncode == 0 and len(body) > 100:\n"
        "    pathlib.Path(sys.argv[-1]).write_text(body + '\\n', encoding='utf-8')\n"
        "    print('周报已写入')\n"
        "else:\n"
        "    print('周报生成失败', r.returncode, (r.stderr or body)[:300])\n",
        *cmd, str(out_path),
    ]
    return _background_job(ws, "weekly", wrapper)


def patrol(project: str) -> str:
    """总 Agent 巡检:对任意项目起一个观测 agent,读工作状况产出督导报告(只读,不动手)。"""
    pr = find_project(project) or find_project(DEFAULT_PROJECT)
    rundir = Path(pr["dir"])
    st = shezha_state() if project == SHEZHA_PROJECT else {}
    if project == SHEZHA_PROJECT and not st.get("configured"):
        return "涉诈批次未绑定作战仓库"
    materials = [f"# 项目:{pr['name']}(profile={pr.get('profile') or '跟随全局'})"]
    if project == SHEZHA_PROJECT:
        materials.append(f"批次:{st.get('batch')} · 本批目标 {len(st.get('targets') or [])} 个")
        rz = shezha_residents()
        zomb = [r["name"] for r in rz["rows"] if r["status"].startswith("运行中") and not r["pid_alive"]]
        materials.append(f"常驻:{sum(1 for r in rz['rows'] if r['status'].startswith('运行中'))} 路运行中"
                         + (f";⚠ 僵尸行:{', '.join(zomb[:10])}" if zomb else ""))
        tl = shezha_timeline(days=3)
        materials.append("近 3 天时间线:\n" + "\n".join(
            f"- [{e['ts']}] ({e['kind']}) {e['site']} {e['text']}" for e in tl[:80]))
    board = read_json(rundir / "board.json", {}).get("questions") or {}
    qlines = [f"- {qid}[{e.get('status')}] 尝试{e.get('attempts', 0)}次 findings{e.get('findings_count', 0)}"
              f" {str(e.get('title') or '')[:40]}"
              for qid, e in list(board.items())[:60]]
    if qlines:
        materials.append("目标/站点:\n" + "\n".join(qlines))
    facts = read_json(rundir / "board.json", {}).get("facts") or []
    if facts:
        materials.append("黑板近况(尾 15 条):\n" + "\n".join(
            f"- {str(f.get('text', ''))[:100]}" for f in facts[-15:]))
    rout = rundir / "logs" / "runner.out"
    if rout.exists():
        try:
            materials.append("runner 日志尾:\n" + rout.read_text(encoding="utf-8", errors="replace")[-2500:])
        except OSError:
            pass
    prompt = (
        "你是这个 AI 作业项目的**总 Agent(督导)**,任务是观测当前工作状况并给出督导报告。"
        "只读材料、不执行任何操作。报告格式(markdown):"
        "# 巡检报告\n① 现状一句话 ② 在跑/卡住的目标(点名,含尝试次数与静默迹象) "
        "③ 风险与异常(僵尸常驻/反复失败/出口问题/正规资产预警) ④ 建议的下一步(按优先级,3 条内)。\n"
        "只陈述材料里的事实,没有的写\"未见\";不要客套。\n\n" + "\n\n".join(materials)[:30000])
    cfg = {**config_mod.DEFAULTS, **config_mod.read_env()}
    provider = (st or {}).get("pi_provider") or cfg.get("PI_PROVIDER") or "rotom"
    model = (st or {}).get("pi_model") or cfg.get("PI_MODEL") or ""
    out_dir = rundir / "logs" / "巡检"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"巡检_{time.strftime('%Y%m%d_%H%M')}.md"
    cmd = ["pi", "-p", "--provider", provider, "--model", model, "--no-session",
           "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files", prompt]
    py = sys.executable
    wrapper = [
        py, "-c",
        "import subprocess,sys,pathlib\n"
        "r = subprocess.run(sys.argv[1:-1], capture_output=True, text=True, timeout=800)\n"
        "body = (r.stdout or '').strip()\n"
        "if len(body) > 80:\n"
        "    pathlib.Path(sys.argv[-1]).write_text(body + '\\n', encoding='utf-8')\n"
        "    print('巡检报告已写入', sys.argv[-1])\n"
        "else:\n"
        "    print('巡检失败', r.returncode, (r.stderr or body)[:300])\n",
        *cmd, str(out_path),
    ]
    return _background_job(rundir, "patrol", wrapper)


def patrol_status(project: str) -> dict:
    pr = find_project(project) or find_project(DEFAULT_PROJECT)
    rundir = Path(pr["dir"])
    status = read_json(rundir / "logs" / "patrol_status.json", None)
    last = None
    pdir = rundir / "logs" / "巡检"
    if pdir.is_dir():
        files = sorted(pdir.glob("巡检_*.md"), key=lambda p: p.stat().st_mtime)
        if files:
            try:
                last = {"path": str(files[-1]),
                        "mtime": time.strftime("%F %T", time.localtime(files[-1].stat().st_mtime)),
                        "content": files[-1].read_text(encoding="utf-8", errors="replace")[:6000]}
            except OSError:
                last = None
    return {"status": status, "last": last}


def save_config(payload: dict) -> str:
    updates = {}
    for k in EDITABLE_KEYS:
        if k in payload and payload[k] is not None:
            updates[k] = str(payload[k]).strip()
    if "PI_BASE_URL" in updates:
        updates["PI_BASE_URL"] = updates["PI_BASE_URL"].rstrip("/")
    if "PI_API_TYPE" in updates and updates["PI_API_TYPE"] not in API_TYPES:
        return f"未知 API 类型: {updates['PI_API_TYPE']}"
    if updates.get("PROFILE"):
        try:                                     # 策略包名写错会让 runner 起不来,先挡住
            import profile as profile_mod
            profile_mod.load(updates["PROFILE"])
        except Exception as e:
            return f"策略包不可用({updates['PROFILE']}): {e}"
    if not updates:
        return "没有可保存的字段"
    config_mod.write_env(updates)
    apply_env()  # 让本进程立即用上新地址
    cfg = {**config_mod.DEFAULTS, **config_mod.read_env()}
    ok = config_mod.sync_provider(cfg, quiet=True)
    mask = {k: ("***" if ("KEY" in k or "TOKEN" in k) else v) for k, v in updates.items()}
    msg = f"已保存: {mask}"
    if ok:
        msg += "; 已同步到 pi provider(rotom)"
    else:
        msg += "; 未同步 provider(缺 base URL 或模型名)"
    running_projects = [pr["name"] for pr in list_projects()
                        if runner_status(Path(pr["dir"]))["running"]]
    if running_projects:
        msg += f"。注意:这些项目的 runner 正在跑,模型/token 变更需重启才生效 → {'、'.join(running_projects)}"
    return msg


def _current_profile(cfg: dict):
    """当前策略包(dashboard 只用来分支显示/检查项,失败返回 None)。"""
    try:
        import profile as profile_mod
        return profile_mod.load((cfg.get("PROFILE") or os.environ.get("PROFILE") or "").strip() or None)
    except Exception:
        return None


def _load_api():
    """按当前配置加载任务源/平台适配器(与 runner 同一套选择逻辑)。

    策略包声明了 task_source(正式渗透 → targets_api)时以它为准,忽略 CONTEST_ADAPTER。
    """
    apply_env()
    prof = _current_profile({**config_mod.DEFAULTS, **config_mod.read_env()})
    if prof is not None and prof.task_source:
        import importlib
        return importlib.import_module(prof.task_source)
    # 适配器: .env 的 CONTEST_ADAPTER 优先;没设时用策略包声明的默认(zhongce → tsec)
    adapter = (os.environ.get("CONTEST_ADAPTER") or (prof.get("adapter") if prof else "") or "").lower()
    if adapter == "tsec":
        import tsec_api
        return tsec_api
    if adapter == "arena":
        import arena_api
        return arena_api
    if adapter == "newrank":
        import newrank_api
        return newrank_api
    import contest_api
    return contest_api


def test_vpn() -> str:
    """靶场 VPN 联通预检(众测流程的前置;策略包可关)。"""
    cfg = {**config_mod.DEFAULTS, **config_mod.read_env()}
    prof = _current_profile(cfg)
    api = _load_api()
    if not hasattr(api, "check_vpn"):
        return "当前适配器不需要 VPN 预检"
    if prof is not None and prof.get("containers.vpn_gate") is False:
        return f"当前策略包({prof.name})未启用 VPN 预检(要启用就改 profiles/{prof.name}.json 的 containers.vpn_gate)"
    r = api.check_vpn()
    if r.get("ok"):
        return f"VPN 已连通:client_ip={r.get('client_ip')}"
    return (f"VPN 未连通({r.get('error')})—— 先连靶场 VPN(平台下发的 .ovpn),"
            f"再点一次「测试 VPN」")


def test_contest() -> str:
    """用当前配置实测平台接口:能不能查到题、能查到几道、进度如何。

    PROFILE=pentest(goal=findings)时没有平台:改为检查本地目标清单 targets.json。
    """
    api = _load_api()
    cfg = {**config_mod.DEFAULTS, **config_mod.read_env()}
    prof = _current_profile(cfg)
    if prof is not None and prof.goal == "findings":
        try:
            qs = api.list_questions("", timeout=10)
        except getattr(api, "ContestError", ()) as e:
            return f"目标清单不可用:{e}"
        except Exception as e:
            return f"目标清单读取失败:{str(e)[:200]}"
        codes = ", ".join(str(q.get("question_id")) for q in qs[:5])
        where = f"(清单:{api.targets_path()})" if hasattr(api, "targets_path") else ""
        return (f"目标清单正常:共 {len(qs)} 个目标{where}"
                f"({codes}{'…' if len(qs) > 5 else ''});正式渗透模式没有平台接口,这是预期行为")
    token = (cfg.get("BENCHMARK_TOKEN") or cfg.get("TEAM_TOKEN") or "").strip()
    if not token:
        return "未配置 BENCHMARK_TOKEN(跑分任务凭证)"
    try:
        qs = api.list_questions(token, timeout=20)
    except getattr(api, "ContestError", ()) as e:
        # 接口通了,但平台业务上拒绝(任务未开始/已结束、token 无效)——与"网络不通"要分开说
        return f"接口通了,平台业务提示:{e}(跑分任务未开始/已结束或 token 无效;任务开始后会自动恢复)"
    except Exception as e:
        return f"接口不通(网络/地址问题): {str(e)[:200]}"
    if not qs:
        return "接口通了,但题目列表为空(可能任务未开始或 token 无对应用例集)"
    done = sum(1 for q in qs if q.get("is_solved"))
    flags = sum(int(q.get("correct_flag_count") or 0) for q in qs)
    total = sum(int(q.get("flag_count") or 1) for q in qs)
    codes = ", ".join(str(q.get("question_id")) for q in qs[:5])
    return (f"接口正常:共 {len(qs)} 道题,已通关 {done} 道,flag {flags}/{total}"
            f"({codes}{'…' if len(qs) > 5 else ''})")


def test_model() -> str:
    cfg = {**config_mod.DEFAULTS, **config_mod.read_env()}
    model = cfg.get("PI_MODEL") or ""
    if not model:
        return "未配置模型名"
    config_mod.sync_provider(cfg, quiet=True)
    env = {**os.environ}
    # 网络策略与 worker 完全一致(直连摘掉代理变量 / 断网走黑洞)
    config_mod.apply_net_policy(env, cfg)
    # 让模型端点绕过本机代理:代理客户端一关(或现场无代理),不带白名单就会 Connection error
    import solver as _solver
    hosts = ["localhost", "127.0.0.1", "::1"] + _solver._provider_hosts(cfg.get("PI_PROVIDER", "")) \
            + ["api.deepseek.com", "api.kimi.com", "www.micuapi.ai"]
    no_proxy = ",".join(dict.fromkeys(hosts))
    cur = env.get("no_proxy", "")
    env["no_proxy"] = env["NO_PROXY"] = (cur + "," + no_proxy).strip(",")
    if cfg.get("PI_API_KEY_VAR") and cfg.get("PI_API_KEY"):
        env[cfg["PI_API_KEY_VAR"]] = cfg["PI_API_KEY"]
    # 模型端点绕过本机代理(本机代理一旦失效,测试会误报不通;比赛现场无代理时不受影响)
    try:
        import json as _json
        mp = Path.home() / ".pi" / "agent" / "models.json"
        host = ((_json.loads(mp.read_text(encoding="utf-8")).get("providers") or {})
                .get(cfg.get("PI_PROVIDER") or "rotom", {}).get("baseUrl") or "").split("//")[-1].split("/")[0].split(":")[0]
        if host:
            env["no_proxy"] = env["NO_PROXY"] = ((env.get("no_proxy") or "") + "," + host).strip(",")
    except Exception:
        pass
    cmd = ["pi", "-p", "--provider", cfg.get("PI_PROVIDER") or "rotom", "--model", model,
           "--no-session", "--no-extensions", "--no-skills", "--no-prompt-templates",
           "--no-context-files", "只回复两个字:正常"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except subprocess.TimeoutExpired:
        return "测试超时(120s):检查 base URL / 网络 / 代理"
    out = (r.stdout or "").strip()
    if r.returncode == 0 and out:
        return f"模型连通正常(model={model})"
    err = (r.stderr or "").strip().splitlines()
    return f"测试失败: {err[-1][:200] if err else '无输出'}"


# ---------------------------------------------------------------- 看板数据

def worker_activity(log_path: str, max_bytes: int = 400_000, n: int = 5) -> dict:
    out = {"actions": [], "speech": "", "tokens": 0, "errors": [], "alive_sec": 0}
    try:
        size = os.path.getsize(log_path)
    except OSError:
        return out
    try:
        with open(log_path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            if size > max_bytes:
                f.readline()
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t = ev.get("type")
        if t == "error":
            msg = str(ev.get("errorMessage", ""))[:200]
            if msg:
                out["errors"].append(msg)
        elif t == "tool_execution_start":
            # 工具参数只在 tool_execution_start 里(assistant 消息里的 toolCall.args 是空的)
            args = ev.get("args") or {}
            brief = args.get("command") or args.get("path") or args.get("pattern") or ""
            brief = " ".join(str(brief).split())[:120]
            name = ev.get("toolName", "?")
            out["actions"].append(f"{name}: {brief}" if brief else name)
        elif t == "message_end":
            msg = ev.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            usage = msg.get("usage") or {}
            out["tokens"] = usage.get("totalTokens") or out["tokens"]
            for c in msg.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text", "").strip():
                    out["speech"] = c["text"].strip().replace("\n", " ")[:200]
    out["actions"] = out["actions"][-n:]
    out["errors"] = out["errors"][-2:]
    out["alive_sec"] = int(time.time() - os.path.getmtime(log_path))
    return out


def shared_board(rundir: Path, entry: dict) -> str:
    workdir = (entry.get("workdir") or "").strip()
    candidates = [rundir / "work" / workdir] if workdir else []
    candidates += [d for d in (rundir / "work").glob("*") if d.is_dir()]
    for d in candidates:
        p = d / "SHARED.md"
        if p.exists():
            return textutil.read_text_lossless(p)[-2500:]
    return ""


def busiest_project() -> str:
    """挑一个"最可能有数据"的项目。

    浏览器 localStorage 里存的项目名可能已经不对(比如数据在 projects/xxx 里,
    而页面还停在"默认项目"),控制台就会一片空白、看着像"日志没进去"。
    所以:请求的项目没有 board.json 时,自动退到最近有更新的那个项目。
    """
    best, best_ts = "", -1.0
    for pr in list_projects():
        if not _count_questions(Path(pr["dir"])):
            continue
        board = Path(pr["dir"]) / "board.json"
        try:
            ts = board.stat().st_mtime
        except OSError:
            continue
        if ts > best_ts:
            best, best_ts = pr["name"], ts
    return best


def build_state(project: str = DEFAULT_PROJECT) -> dict:
    pr = find_project(project) or find_project(DEFAULT_PROJECT)
    if project != SHEZHA_PROJECT and not _count_questions(Path(pr["dir"])):   # 这个项目是空的 → 换到有数据的项目
        alt = busiest_project()
        if alt and alt != pr["name"]:
            pr = find_project(alt) or pr
    project = pr["name"]
    rundir, logs = Path(pr["dir"]), Path(pr["dir"]) / "logs"
    board = read_json(rundir / "board.json", {"questions": {}, "facts": []})
    ext = external_runner(rundir)          # EXTERNAL_RUNNER:容器/别的机器在跑这个数据目录
    reg = [w for w in read_json(logs / "workers.json", []) if pid_alive(w.get("pid"))]
    if ext:
        # 外部 runner(容器模式)下 workers.json 里的 pid 是**容器内**的,在宿主机上查不到存活 ——
        # 不能按 pid 过滤(会显示 0 个 worker、把 running 的题误标成「已中断」,实测让用户以为全死了)。
        # 直接用在册列表:jsonl 日志就挂在宿主机上,活动/沉默时间照样算得准。
        reg = read_json(logs / "workers.json", []) or []
        # 日志路径也是**容器内**的(如 /rotrun/logs/x.jsonl),宿主机上不存在 → 按标记里记的 run_dir 映射回来,
        # 否则 worker_activity() 会因为文件不存在直接早退,看板上"最近动作"永远是空的(踩过)。
        cdir = str((read_json(logs / EXTERNAL_RUNNER, {}) or {}).get("run_dir") or "").rstrip("/")
        if cdir:
            reg = [{**w, "log": str(rundir) + str(w.get("log", ""))[len(cdir):]
                    if str(w.get("log", "")).startswith(cdir) else w.get("log", "")} for w in reg]
    runner_running = runner_status(rundir)["running"] or bool(ext)
    questions = []
    for qid, e in board.get("questions", {}).items():
        acts = []
        for w in reg:
            if w.get("qid") != qid:
                continue
            a = worker_activity(w.get("log", ""), n=4)
            acts.append({"slot": w.get("slot"), "age": int(time.time() - w.get("started", time.time())),
                         "actions": a["actions"], "speech": a["speech"], "errors": a["errors"],
                         "quiet": a["alive_sec"], "tokens": a["tokens"]})
        started = e.get("started_at")
        status, label = e.get("status", "pending"), None
        if status == "running" and not runner_running and not acts:
            label = "已中断"  # runner 被杀后的残留状态,别显示成还在跑
        questions.append({
            "qid": qid, "title": e.get("title", qid), "category": e.get("category", ""),
            "score": e.get("score", 0), "status": status,
            "status_label": label or STATUS_LABEL.get(status, status),
            "status_class": "warn" if label else STATUS_CLASS.get(status, ""),
            "attempts": e.get("attempts", 0), "max_attempts": e.get("max_attempts", 0),
            "unlimited": (e.get("max_attempts", 0) or 0) <= 0,
            "elapsed": int(time.time() - started) if started and e.get("status") == "running" else None,
            "connection": e.get("connection") or {}, "workdir": e.get("workdir") or "",
            "description": (e.get("description") or "")[:200],
            # 一题多 flag:看板显示 x/y,而不是只看"解出/未解出"
            "flag_count": int(e.get("flag_count") or 1),
            "correct_flag_count": int(e.get("correct_flag_count") or 0),
            "flag_progress": (f"{int(e.get('correct_flag_count') or 0)}/{int(e.get('flag_count') or 1)}"
                              if int(e.get("flag_count") or 1) > 1 else ""),
            "container_status": e.get("container_status") or "",
            "container_ready": bool(e.get("container_ready", True)),
            "level": e.get("level") or "", "difficulty": e.get("difficulty") or "",
            "workers": acts,
            # 这里**不再**每题读一遍 SHARED.md:63 道题每 2 秒各读一次文件纯属浪费。
            # 黑板又长又杂,只在"单题详情"按需取(见 /api/question)。
        })
    order = {"running": 0, "pending": 1, "failed": 2, "solved": 3}
    questions.sort(key=lambda q: (order.get(q["status"], 9), -q["score"]))

    subs = []
    sp = logs / "submissions.log"
    if sp.exists():
        for line in sp.read_text(encoding="utf-8", errors="ignore").strip().splitlines()[-12:]:
            try:
                subs.append(json.loads(line))
            except Exception:
                pass
    subs.reverse()
    rstate = read_json(logs / "runner_state.json", {})
    stale = False
    if rstate.get("heartbeat"):
        stale = (time.time() - float(rstate["heartbeat"])) > 60   # 60 秒没心跳=可能卡死
    residents = None
    if project == SHEZHA_PROJECT:      # 涉诈批次:常驻汇总(双层展示的项目层)
        res = shezha_residents()
        residents = {"groups": res["groups"],
                     "running": sum(1 for r in res["rows"] if r["status"].startswith("运行中")),
                     "zombie": sum(1 for r in res["rows"]
                                   if r["status"].startswith("运行中") and not r["pid_alive"])}
    return {
        "now": time.strftime("%H:%M:%S"),
        "solved": sum(1 for q in questions if q["status"] == "solved"), "total": len(questions),
        "flags": sum(q["correct_flag_count"] for q in questions),
        "total_flags": sum(q["flag_count"] for q in questions),
        # 涉诈批次:findings 总数 + 常驻汇总(项目层,站点层见 /api/question 的 site)
        "findings": sum(int(e.get("findings_count") or 0) for e in board.get("questions", {}).values()),
        "residents": residents,
        "running_workers": len(reg), "paused": bool(rstate.get("paused")),
        "limit": rstate.get("limit"),
        # runner:外部 runner(容器)在跑时也要如实标 running —— 否则前端把在场题显示成「已中断」(踩过)
        "runner": {**runner_status(rundir), "running": runner_running, "external": ext},
        "external_runner": ext,
        "runner_stale": stale, "time_left": rstate.get("time_left"), "logs_mb": rstate.get("logs_mb"),
        "project": project, "projects": list_projects(),
        "config": {**config_mod.DEFAULTS, **config_mod.read_env()},
        "facts": list(reversed(board.get("facts", [])[-25:])),
        "questions": questions, "submissions": subs,
    }

# ---------------------------------------------------------------- 解题链/收获/知识库

# SHARED.md 的事件类型分类:借鉴 StrikeAgent 时间线的事件标签,
# 把自由文本归类成"已确认/计划/进展/增援/失败/Observer/系统"7 种,前端配色不一样。
SHARED_KIND_PATTERNS = [
    ("confirm",  re.compile(r"已确认|确认|找到|拿到|flag\s*[=:]|得到了|复原|解码|exp\s*可用|工作")),
    ("plan",     re.compile(r"计划|打算|准备|将从|我将|我要|我会|入手")),
    ("progress", re.compile(r"进展|开始|正在|继续|检查|尝试|测试|验证|提取|匹配|解析|看\s*\w")),
    ("rescue",   re.compile(r"请求增援|需要.*agent|招人|加人")),
    ("fail",     re.compile(r"失败|不行|不行|超时|卡住|放弃|错|无果|没用|跑不通")),
    ("obs",      re.compile(r"^\s*\[Observer\s")),
    ("sys",      re.compile(r"^\s*\[(系统|Observer|harness|runner|pi)\b")),
]


def classify_shared_line(line: str) -> str:
    """给一行 SHARED.md 标注类型;用关键词粗分类,匹配不到默认 progress。"""
    for kind, pat in SHARED_KIND_PATTERNS:
        if pat.search(line):
            return kind
    return "progress"


_SHARED_LINE_RE = re.compile(
    r"^-\s+\[(?P<tag>[^\]]+)\]\s+(?P<body>.*)$"
)


def parse_shared_timeline(rundir: Path, entry: dict) -> list[dict]:
    """解析 SHARED.md,返回 [{slot,time,tag,kind,text},...],按文件顺序。

    没有 SHARED.md 返回空列表。行格式参考:[w1 15:30] 事实/结论...
    取不到 slot 就记 'sys'。"""
    workdir = (entry.get("workdir") or "").strip()
    candidates = [rundir / "work" / workdir] if workdir else []
    candidates += [d for d in (rundir / "work").glob("*") if d.is_dir()]
    path = next((d / "SHARED.md" for d in candidates if (d / "SHARED.md").exists()), None)
    if path is None:
        return []
    out: list[dict] = []
    for raw in textutil.read_text_lossless(path).splitlines():
        m = _SHARED_LINE_RE.match(raw.strip())
        if not m:
            continue
        tag = m.group("tag").strip()
        body = m.group("body").strip()
        # tag 形如 "w1 15:30 进展" / "w1 计划" / "Observer 15:52" →
        # 第二段若像 HH:MM 就当时间,否则当 label;w\d+ 才是 slot,其它都归 sys
        parts = tag.split(None, 2)
        slot = parts[0] if parts else "sys"
        if not re.match(r"^w\d+$", slot):
            slot = "sys"
        rest = parts[1:]
        t = ""
        label = ""
        if len(rest) == 1:
            if re.match(r"^\d{1,2}:\d{2}$", rest[0]):
                t = rest[0]
            else:
                label = rest[0]
        elif len(rest) >= 2:
            t = rest[0]
            label = rest[1]
        # 拼回 kind = 既有分类标签 + 原文标签里也可能带(进展/计划/已确认)
        kind = classify_shared_line(body)
        if "确认" in label or "完成" in label:
            kind = "confirm"
        elif "增援" in label:
            kind = "rescue"
        elif "计划" in label:
            kind = "plan"
        elif "失败" in label or "放弃" in label:
            kind = "fail"
        out.append({"slot": slot, "time": t, "label": label, "kind": kind, "text": body,
                    "line_no": len(out) + 1})
        # Observer 跟踪行不参与"已确认/收获",只是观测量,免得被"找到|flag"误判成 confirm
        if slot == "sys" and (label.startswith("Observer") or "Observer" in (parts[0] if parts else "")):
            out[-1]["kind"] = "obs"
        # 显式 label 已确认/完成 → 提升为 confirm,无视正文分类
        if "已确认" in label or "完成确认" in label:
            out[-1]["kind"] = "confirm"
    return out


# 严重度关键词:粗糙的规则,目的是给"收获"面板一个排序,不是真评分
FINDING_SEVERITY_RULES = [
    ("critical", re.compile(r"flag\s*[=:]|已解出|拿到\s*flag|提交成功|CORRECT|submitted")),
    ("high",     re.compile(r"已确认|确认\s*[：:]|exp\s*可用|exp\s*跑通|复原成功|解码成功|注入成功|命中")),
    ("medium",   re.compile(r"线索|可疑|疑似|发现\s*\w|摸到|识别到|匹配到|响应|包\s*\w")),
    ("low",      re.compile(r"读\s*\w|查看|列出|记录|备份|版本|路径|环境")),
]


def finding_severity(text: str) -> str:
    """对一条 SHARED 文本估算严重度;默认 medium(让 UI 别一片空白)。"""
    for sev, pat in FINDING_SEVERITY_RULES:
        if pat.search(text):
            return sev
    return "medium"


_ARTIFACT_GLOBS = ("*.py", "*.sh", "*.md", "*.txt", "*.exp", "*.bin", "*.json", "*.html")
_ARTIFACT_SKIP = {"SHARED.md"}


def collect_artifacts(rundir: Path, entry: dict) -> list[dict]:
    """枚举本题工作区下每个 w* 子目录写下的产物,供'收获'面板展示。

    只看一级 + 不递归(避免把 payload/test 目录也吃掉);按 mtime 倒序。"""
    workdir = (entry.get("workdir") or "").strip()
    base = rundir / "work" / workdir if workdir else None
    if not base or not base.exists():
        return []
    out: list[dict] = []
    for slot_dir in sorted(base.glob("w*")):
        if not slot_dir.is_dir():
            continue
        slot = slot_dir.name
        for glob in _ARTIFACT_GLOBS:
            for p in slot_dir.glob(glob):
                if p.name in _ARTIFACT_SKIP or not p.is_file():
                    continue
                try:
                    sz = p.stat().st_size
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                out.append({"slot": slot, "path": str(p.relative_to(base)),
                            "name": p.name, "size": sz,
                            "mtime": time.strftime("%m-%d %H:%M", time.localtime(mt))})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def build_findings(rundir: Path, entry: dict) -> dict:
    """整合本题'收获':已确认条目 + worker 产物。"""
    timeline = parse_shared_timeline(rundir, entry)
    confirmed = [e for e in timeline if e["kind"] == "confirm"]
    findings = []
    for e in confirmed:
        text = e["text"]
        sev = finding_severity(text)
        findings.append({"slot": e["slot"], "time": e["time"], "severity": sev,
                         "text": text[:280], "line_no": e["line_no"]})
    # 失败条目也保留,但用更弱的样式
    fails = [{"slot": e["slot"], "time": e["time"], "text": e["text"][:280],
             "line_no": e["line_no"]} for e in timeline if e["kind"] == "fail"]
    artifacts = collect_artifacts(rundir, entry)
    return {"findings": findings, "fails": fails[-10:],
            "artifacts": artifacts, "timeline_count": len(timeline)}


_KNOWLEDGE_NAMES = ("web", "pwn", "reverse", "crypto", "forensics", "misc")


def knowledge_root() -> Path:
    """知识库目录:代码根的 knowledge/。失败回退到相对路径,避免导入期崩。"""
    here = Path(__file__).resolve().parent
    cand = here / "knowledge"
    return cand if cand.exists() else Path.cwd() / "knowledge"


def list_knowledge() -> list[dict]:
    """列知识库文件:主目录 6 个方向 md + vendor 顶层 + vendor/RsaCtfTool/README 等。"""
    root = knowledge_root()
    out: list[dict] = []
    for p in sorted(root.glob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        out.append({"path": p.name, "kind": "playbook",
                    "name": p.stem, "size": p.stat().st_size,
                    "abs": str(p)})
    v = root / "vendor"
    if v.exists():
        for sub in sorted(v.iterdir()):
            if not sub.is_dir():
                continue
            rdm = sub / "README.md"
            if rdm.exists():
                out.append({"path": f"vendor/{sub.name}/README.md", "kind": "vendor",
                            "name": sub.name, "size": rdm.stat().st_size,
                            "abs": str(rdm)})
    return out


def read_knowledge(rel: str, max_bytes: int = 12000) -> str:
    """读一个知识库文件内容(限制大小,前端不卡)。rel 校验防止越界。"""
    root = knowledge_root()
    rel = (rel or "").lstrip("/").replace("..", "")
    if not rel:
        return ""
    target = (root / rel).resolve()
    if not str(target).startswith(str(root.resolve())):
        return "非法路径"
    if not target.exists() or not target.is_file():
        return ""
    try:
        return target.read_text(encoding="utf-8", errors="ignore")[:max_bytes]
    except Exception as e:
        return f"读取失败: {e}"


def search_kb(query: str, limit: int = 12) -> str:
    """调 tools/kb.py 跑一次 search;失败/未配置时降级到子串 grep。"""
    q = (query or "").strip()
    if not q:
        return ""
    root = Path(__file__).resolve().parent
    kb = root / "tools" / "kb.py"
    if kb.exists():
        try:
            r = subprocess.run([sys.executable, str(kb), "search", q],
                               capture_output=True, text=True, timeout=8,
                               cwd=str(root))
            out = (r.stdout or "").strip()
            if out:
                lines = out.splitlines()
                return "\n".join(lines[:limit]) + ("\n…" if len(lines) > limit else "")
        except Exception:
            pass
    # 兜底:对 6 个方向 md 跑 grep
    kdir = knowledge_root()
    results = []
    pat = re.compile(re.escape(q), re.IGNORECASE)
    for p in kdir.glob("*.md"):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if pat.search(line):
                results.append(f"{p.name}:{i}: {line[:160]}")
                if len(results) >= limit:
                    break
        if len(results) >= limit:
            break
    return "\n".join(results) or "无匹配"


# ---------------------------------------------------------------- 攻击链路图

# 节点类型(对标 StrikeAgent 的 target/service/danger/credential/foothold/goal/info):
# 在 CTF 上下文里映射成 target / endpoint(探测到的服务或端点) / tool(用过的工具)
# / script(写下的脚本) / finding(已确认发现) / goal(已拿到 flag)。
NODE_KINDS = ("target", "endpoint", "tool", "script", "finding", "goal")

# endpoint 抽取规则:从 bash 命令里抓 URL、IP:PORT、域名。规则尽量宽松,目的是出图,不是分类。
_ENDPOINT_PATTERNS = [
    re.compile(r"https?://[^\s'\"<>]+"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b"),
    re.compile(r"\bnc\s+[\w.\-]+\s+\d{1,5}\b"),
]
# tool 抽取:从 command 第一段抓(去掉绝对路径)
_TOOL_RE = re.compile(r"(?:^|[\s/])([a-zA-Z][\w.\-]{1,20})(?=\s|$)")


def _shorten(s: str, n: int = 60) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


_IP_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?$")
_TRAIL = "\"'`,;)]}>\u3002,、;:"


def norm_endpoint(s: str) -> str:
    """把抓到的 endpoint 归一成 `host[:port]`,抓不到真主机就返回空串。

    为什么要归一:同一个目标会以 5 种写法出现在命令里 ——
    `http://10.0.174.184/a.php`、`10.0.174.184:80`、`10.0.174.184:80;`、`10.0.174.184`、`$ip:80/…`。
    不归一的话一道题会冒出几十个"不同"端点(实测 b-03:42 个,实际只有十来个主机)。
    另外 `$ip` 这类 shell 变量不是真主机,直接丢掉。
    """
    s = (s or "").strip().strip(_TRAIL)
    if not s:
        return ""
    if "://" in s:
        s = s.split("://", 1)[1]
    if "nc " in s or s.startswith("nc "):
        s = s.split()[-2] + ":" + s.split()[-1]
    s = s.split("/", 1)[0].strip(_TRAIL)          # 去掉路径
    if s.startswith("$") or s.startswith("{"):    # shell 变量不是主机
        return ""
    if s.startswith("-"):                          # `curl -w:5` 这类命令参数被正则误抓 → 丢掉
        return ""
    host, _, port = s.rpartition(":")
    if not host:                                   # 没有冒号
        host, port = s, ""
    if not re.match(r"^[\w.\-]+$", host):          # 主机名/IP 之外的字符 → 丢弃
        return ""
    if port and not port.isdigit():
        return ""
    if port == "80":                               # http 默认端口: `host:80` 与 `host` 是同一个目标
        port = ""
    return f"{host}:{port}" if port else host


def _split_host_port(ep: str) -> tuple[str, str]:
    """`10.0.1.5:8080` → ("10.0.1.5", "8080");没写端口返回 ("10.0.1.5", "")。

    链路图按主机建点(同一台机器换端口不该长成两个点),端口作为点的属性显示,
    所以这里只负责把归一后的 endpoint 拆开。
    注意:没冒号时 `rpartition(":")` 会把整串塞进第三段,必须先判有没有冒号。
    """
    ep = (ep or "").strip()
    if ":" not in ep:
        return ep, ""
    host, _, port = ep.rpartition(":")
    return (host or ep, port)


def _label_for_tool(name: str) -> str:
    n = name.split("/")[-1]
    if n in {"ls", "cat", "echo", "cd", "pwd", "true", "false", "grep", "head", "tail", "wc",
              "cp", "mv", "rm", "mkdir", "touch", "sleep", "env", "printf", "xargs"}:
        return ""
    return n


def _extract_endpoints_and_tools(cmd: str) -> tuple[list[str], list[str]]:
    """从一条 bash 命令里抓端点和服务端。返回 (endpoints, tools)。"""
    if not cmd:
        return [], []
    endpoints: list[str] = []
    for pat in _ENDPOINT_PATTERNS:
        for m in pat.findall(cmd):
            n = norm_endpoint(m)
            if n:
                endpoints.append(n)
    # 工具名(取命令前缀 token,过滤常见 shell builtin)
    first = cmd.strip().split(None, 1)
    tool = ""
    if first and first[0] not in {"|", "&&", "||", ";", ">"}:
        tool = _label_for_tool(first[0].split("/")[-1])
    return list(dict.fromkeys(endpoints)), ([tool] if tool else [])


def _intent_of(thinking: str, limit: int = 40) -> str:
    """把 agent 的思考压成"这一步要干什么"的一句话,给链路图的工具框当副标题。

    链路图原来只显示工具名(`python3`、`oracle.py`),看不出"为什么用它" ——
    其实 pi 的 jsonl 里每个 toolCall 都和它前面那段 thinking 在**同一条 turn_end 消息**里
    (`content` = [thinking/text, toolCall,…],id 与 tool_execution_start 的 toolCallId 相同),
    意图可以直接取模型自己的话,不用再调一次模型。

    **取末句而不是首句**(实测 b-03 十条配对,九条如此):模型把结论写在开头、把"下一步"
    写在结尾 —— 首句常是上一步的结论(`No SQLi on username likely.` / `No weak creds.`),
    末句才是目的(`Let me write poc.py to test login injection.`)。
    """
    s = " ".join((thinking or "").split())
    if not s:
        return ""
    parts = [p.strip() for p in re.split(r"(?<=[。.!?;；])\s+", s) if p.strip()]
    # 从后往前挑第一条"有信息量"的句子:模型的思考常以 `do that.` / `write it.` 这种
    # 承接句收尾,直接取末句会得到没意义的副标题。
    pick = ""
    for p in reversed(parts or [s]):
        if len(p) >= 12:
            pick = p
            break
    pick = pick or (parts[-1] if parts else s)
    # 末句通常带这些口头语,去掉后当副标题更短更清楚
    pick = re.sub(r"^(I'll|I will|Let me|Now I|Next,?|OK,?|Alright,?|Then|So)\s+", "", pick, flags=re.I)
    return pick.strip(" 　.")[:limit]
    return s[:limit]


def _walk_tool_events(log_path: Path, max_bytes: int = 600_000) -> list[dict]:
    """从一条 worker jsonl 里抽 tool_execution_{start,end} 事件,按时间成对配。

    顺带把每个 toolCall 对应的 thinking 取出来(同一条 turn_end 消息里),
    作为"这一步要干什么"的意图,给链路图当副标题。
    """
    try:
        size = log_path.stat().st_size
    except OSError:
        return []
    try:
        with open(log_path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            if size > max_bytes:
                f.readline()
            lines = f.read().decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    starts: dict[str, dict] = {}
    ends: dict[str, dict] = {}
    intents: dict[str, str] = {}      # toolCallId -> agent 自己写的这一步意图
    out: list[dict] = []
    # 必须**先扫完整段再配对**:pi 的写入顺序是 tool_execution_end → turn_end,
    # 而意图在 turn_end 里 —— 边扫边配对的话,配到 tool 时 turn_end 还没读到,
    # 意图永远是空(实测就是栽在这里)。starts 是有序 dict,后面按它遍历即时间序。
    for line in lines:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t = ev.get("type")
        cid = ev.get("toolCallId")
        if t == "turn_end":
            think, text, calls = "", "", []
            for c in (ev.get("message") or {}).get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "thinking" and (c.get("thinking") or "").strip():
                    think = c["thinking"]
                elif c.get("type") == "text" and (c.get("text") or "").strip():
                    text = c["text"]
                elif c.get("type") == "toolCall" and c.get("id"):
                    calls.append(c["id"])
            said = think or text           # 多数轮次有 thinking,少数只有正文
            if said:
                for k in calls:
                    intents[k] = said
        elif t == "tool_execution_start" and cid:
            starts[cid] = ev
        elif t == "tool_execution_end" and cid:
            ends[cid] = ev

    for cid, s in starts.items():
        ev = ends.get(cid)
        if ev is None:                     # 只有 start(日志被截断/正在跑),跳过
            continue
        args = s.get("args") or {}
        cmd = args.get("command") or args.get("path") or args.get("pattern") or ""
        name = s.get("toolName", "?")
        res = ev.get("result") or {}
        is_err = bool(res.get("isError"))
        txt = ""
        for c in res.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                txt = c.get("text", "")
                break
        out.append({"tool": name, "cmd": cmd, "is_error": is_err,
                    "intent": _intent_of(intents.get(cid, "")),
                    "output_preview": _shorten(txt, 80)})
    return out


_EDGE_RANK = {"win": 0, "find": 1, "probe": 2, "use": 3, "write": 4, "read": 5}


def _prune_edges(edges: list[dict]) -> list[dict]:
    """每个节点只保留一条"主干入边",避免同一节点被多条边连成蜘蛛网。

    借鉴参考项目 StrikeAgent 的 chainDisplayEdges:图是给人看的,不是数据导出 ——
    service/info/finding 同时被好几条边指向时,肉眼分不出主线。
    优先级 win > find > probe > use > write > read,同级取先出现的。
    """
    best: dict[str, tuple[int, int]] = {}
    for i, e in enumerate(edges):
        if e.get("from") == e.get("to"):
            continue
        r = _EDGE_RANK.get(str(e.get("kind") or ""), 9)
        cur = best.get(e["to"])
        if cur is None or r < cur[0]:
            best[e["to"]] = (r, i)
    return [edges[i] for _, i in sorted(best.values(), key=lambda x: x[1])]


def build_attack_graph(rundir: Path, entry: dict) -> dict:
    """组装一题的攻击链路图数据:节点、边、统计、目标信息。

    数据流:
      entry(question metadata) → target 节点 + 目标信息卡
      workers.json(活跃 worker 列表) → 决定是否在跑、读哪些 jsonl
      每个 worker 的 jsonl tool 事件 → endpoint / tool / script 节点 + 顺序边
      SHARED.md 已确认 + 提交成功 → finding / goal 节点(可能多条路径的终点)

    节点 id 用 "k:n" 全局自增,前端拿 id 排序定位坐标。"""
    qid = entry.get("question_id", "")
    nodes: list[dict] = []
    edges: list[dict] = []
    nid = 0

    # 去重台账:(kind, dedup_key) → 节点 id。
    # 必须**先查台账再建节点**:agent 会对同一个目标反复 curl/nmap,
    # 每个 URL 都新建节点的话一道题能到 100+ 个节点(实测 b-03:130 个,其中 109 个是同一台主机)。
    #
    # dedup_key 的含义按节点类型分两种(踩过的坑):
    #   endpoint —— 用**主机**做键(不带 slot、不带端口):同一台主机被 w1/w2 探过、或换了端口,
    #               都只该是一个点。之前把 slot 放进键里,同一台机器会按 agent 数复制成 2~3 个点
    #               (实测 a-05 只有 2 台主机却画出 4 个端点节点),图上看着像多台机器。
    #   tool/script —— 键里**保留 slot**:那是某个 agent 的动作/它自己写的脚本,不是目标的事实。
    made: dict[tuple[str, str], str] = {}

    def add_node(kind: str, label: str, slot: str = "", severity: str = "info",
                 detail: str = "", time: str = "", dedup: str | None = None,
                 sub: str = "") -> str:
        nonlocal nid
        label = label[:48]
        key = (kind, (dedup if dedup is not None else label))
        if key in made:
            hit = nodes[int(made[key][1:]) - 1]
            if severity != "info" and hit["severity"] == "info":   # 同类节点升级严重度
                hit["severity"] = severity
            if detail and len(detail) > len(hit["detail"]):
                hit["detail"] = detail[:280]
            if sub and sub not in hit["sub"]:                      # 端口等属性累加
                hit["sub"] = ((hit["sub"] + " " + sub).strip())[:60]
            if slot and slot not in hit["slots"]:                  # 记下还有哪个 agent 碰过
                hit["slots"].append(slot)
            return made[key]
        nid += 1
        node_id = f"n{nid}"
        nodes.append({"id": node_id, "kind": kind, "label": label,
                      "slot": slot, "slots": [slot] if slot else [],
                      "severity": severity, "sub": sub[:60],
                      "detail": detail[:280], "time": time})
        made[key] = node_id
        return node_id

    # target(题目本身)
    conn = entry.get("connection") or {}
    conn_str = ""
    if isinstance(conn, dict):
        for k, v in conn.items():
            if v:
                conn_str += f"{k}={v}  "
    target_detail = (entry.get("description") or "")[:200]
    if conn_str:
        target_detail = (target_detail + "  ·  连接: " + conn_str).strip(" ·")
    target_id = add_node("target", f"{qid}", "", "info", target_detail)

    # 解析 SHARED.md 已确认条目
    timeline = parse_shared_timeline(rundir, entry)
    confirmed = [e for e in timeline if e["kind"] == "confirm"]

    # 解析每个 slot 的 worker 日志
    logs_dir = rundir / "logs"
    workdir = (entry.get("workdir") or "").strip()
    slot_endpoints: dict[str, list[str]] = {}   # slot -> [endpoint_id]
    slot_tools: dict[str, list[str]] = {}
    slot_scripts: dict[str, list[str]] = {}
    slot_last_node: dict[str, str] = {slot: target_id for slot in ("w1", "w2", "w3", "w4")}

    # 用真实 qid 匹配 jsonl 文件名(平台给的 qid,不是 workdir 名)
    if logs_dir.exists():
        # 优先按 qid 前缀;若 qid 形如"misc-misc01"没人写过日志,就回退到 workdir(实测里两者常不同)
        prefixes = [qid, workdir] if workdir and workdir != qid else [qid]
        seen = set()
        for prefix in prefixes:
            for p in sorted(logs_dir.glob(f"{prefix}_w*.jsonl")):
                if str(p) in seen:
                    continue
                seen.add(str(p))
                fname = p.stem
                m = re.search(r"_w(\d+)_", "_" + fname + "_")
                slot = f"w{m.group(1)}" if m else "w?"
                if slot not in slot_last_node:
                    slot_last_node[slot] = target_id
                events = _walk_tool_events(p)
                for ev in events:
                    tool, cmd = ev["tool"], ev["cmd"]
                    intent = ev.get("intent") or ""
                    # bash → 抽 endpoints + tools
                    if tool == "bash":
                        eps, tls = _extract_endpoints_and_tools(cmd)
                        for ep in eps:
                            host, port = _split_host_port(norm_endpoint(ep) or ep)
                            ep_id = add_node("endpoint", host, slot, "info",
                                             detail=ep, time="",
                                             dedup=host,                 # 按主机合并(不带 slot/端口)
                                             sub=(f":{port}" if port else ""))
                            if ep_id not in slot_endpoints.setdefault(slot, []):
                                slot_endpoints[slot].append(ep_id)
                                edges.append({"from": slot_last_node[slot], "to": ep_id,
                                              "kind": "probe"})
                            slot_last_node[slot] = ep_id
                        for tn in tls:
                            # 框里是工具名,框下面挂 agent 自己写的"这一步要干什么"(没有就用命令兜底)
                            t_id = add_node("tool", tn, slot, "info",
                                            detail=(intent + "\n\n$ " + cmd[:200]).strip(),
                                            sub=intent or _shorten(cmd, 26))
                            if t_id not in slot_tools.setdefault(slot, []):
                                slot_tools[slot].append(t_id)
                            edges.append({"from": slot_last_node[slot], "to": t_id,
                                          "kind": "use"})
                            slot_last_node[slot] = t_id
                    elif tool in ("write", "edit"):
                        path = (cmd or "").strip()
                        if path and not path.endswith((".log", ".out", ".tmp")):
                            s_id = add_node("script", path.split("/")[-1], slot,
                                            "medium",
                                            detail=(intent + "\n\n" + cmd[:200]).strip(),
                                            sub=intent or _shorten(cmd, 26))
                            slot_scripts.setdefault(slot, []).append(s_id)
                            edges.append({"from": slot_last_node[slot], "to": s_id,
                                          "kind": "write"})
                            slot_last_node[slot] = s_id
                    elif tool == "read":
                        path = (cmd or "").strip()
                        if path and "SHARED" not in path and not path.endswith(".jsonl"):
                            r_id = add_node("tool", "read", slot, "low",
                                            detail=(intent + "\n\n" + path[:200]).strip(),
                                            sub=intent or _shorten(path.split("/")[-1], 26))
                            edges.append({"from": slot_last_node[slot], "to": r_id,
                                          "kind": "read"})
                            slot_last_node[slot] = r_id

    # finding 节点:从 SHARED.md 已确认条目
    finding_ids: list[str] = []
    for cf in confirmed:
        sev = finding_severity(cf["text"])
        f_id = add_node("finding", _shorten(cf["text"], 48), cf["slot"] or "sys",
                        sev, detail=cf["text"][:280], time=cf["time"])
        finding_ids.append(f_id)
        # 接在 slot 当前末尾;若 slot 不在已扫描列表(没有 worker 日志)就直接连 target
        slot = cf["slot"] if cf["slot"] in slot_last_node else ""
        if slot and slot_last_node.get(slot):
            edges.append({"from": slot_last_node[slot], "to": f_id, "kind": "find"})
        else:
            edges.append({"from": target_id, "to": f_id, "kind": "find"})

    # goal 节点:从 submissions.log 收集**本题所有**成功提交
    #  一题多 flag(实测 b-02 有 6 个)早期只取第一条(还带个 break)→ 图上永远只画一个 FLAG,
    #  多 flag 题看不出任何进度。
    flag_records: list[tuple[str, str]] = []      # [(时间, flag)]
    sp = logs_dir / "submissions.log"
    if sp.exists():
        for line in sp.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("question_id") == qid and rec.get("ok"):
                ts, fl = str(rec.get("ts") or ""), str(rec.get("flag") or "")
                if fl and fl not in [f for _, f in flag_records]:   # 同一 flag 重复提交不重复画
                    flag_records.append((ts, fl))
    total_flags = max(1, int(entry.get("flag_count") or 1))
    flag_text = flag_records[-1][1] if flag_records else ""
    goal_ids: list[str] = []
    for idx, (ts, fl) in enumerate(flag_records):
        label = f"FLAG {idx + 1}/{total_flags} ✓" if total_flags > 1 else "FLAG ✓"
        g_id = add_node("goal", label, "", "critical",
                        detail=fl[:200], time=ts[11:19] if len(ts) >= 19 else ts)
        goal_ids.append(g_id)
        # 第一个 flag 接最近的 finding(没有 finding 就直接接 target);后面的 flag 串在前一个之后
        src = goal_ids[-2] if len(goal_ids) > 1 else (finding_ids[-1] if finding_ids else target_id)
        edges.append({"from": src, "to": g_id, "kind": "win"})

    # 节点统计(供顶部 stat 条)
    sev_count = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for n in nodes:
        sev_count[n["severity"]] = sev_count.get(n["severity"], 0) + 1
    slots = sorted({n["slot"] for n in nodes if n["slot"]})
    target_card = {
        "qid": qid,
        "title": entry.get("title", qid),
        "category": entry.get("category", ""),
        "score": entry.get("score", 0),
        "status": entry.get("status", "pending"),
        "is_solved": bool(entry.get("is_solved")),
        "description": (entry.get("description") or "")[:200],
        "connection": conn if isinstance(conn, dict) else {},
        "workdir": workdir,
        # 统计只放真实计数。曾经这里有个"推测解题概率 45%",是拍脑袋加权出来的假指标,已删。
        "stats": {"nodes": len(nodes),
                  "endpoints": len([n for n in nodes if n["kind"] == "endpoint"]),
                  "tools": len([n for n in nodes if n["kind"] == "tool"]),
                  "scripts": len([n for n in nodes if n["kind"] == "script"]),
                  "findings": len(finding_ids), "critical": sev_count["critical"],
                  "high": sev_count["high"], "medium": sev_count["medium"],
                  "low": sev_count["low"], "slots": len(slots)},
    }
    # 右栏"发现的服务"列表:所有 endpoint + tool 节点
    services = [n for n in nodes if n["kind"] in ("endpoint", "tool", "script")]
    return {"target": target_card, "nodes": nodes, "edges": _prune_edges(edges),
            "services": services, "slots": slots, "flag": flag_text,
            "flags": [f for _, f in flag_records]}


# ---------------------------------------------------------------- 单题组装

def graph_mode_for(entry: dict, graph: dict | None) -> str:
    """这道题该不该画"攻击链路图" —— 由 **agent 实际干了什么** 决定,而不是靠分类猜。

    链路图表达的是"从哪个入口、经过什么服务、怎么打进去、拿到什么",只有**网络渗透**才有这条链。
    判据用探测到的"端点"节点:
      attack      探测到端点(agent 真的在打 10.x:port)→ 画完整链路图
      placeholder 有节点但没有端点(只有脚本/发现/工具)→ 这是"写脚本 → 出结果"的题型
                  (逆向/密码/二进制),链路不是它的形状,先给占位卡
      waiting     还什么都没有(题还没开始跑)→ 给空态,等它自己长出来

    之前用 category 判断是不行的:众测里每题都挂着一个 web 容器,而且关键词分类会把
    "1panel 管理面板"这种 web 题误判成 crypto(实测)。
    """
    nodes = (graph or {}).get("nodes") or []
    if any(n.get("kind") == "endpoint" for n in nodes):
        return "attack"
    if nodes:
        return "placeholder"
    return "waiting"


def build_question(rundir: Path, entry: dict) -> dict:
    """一道题的全部详情数据:前端点一次任务只拉这一次。

    以前是三个接口各自"一次算全部题"(每题还要读多个 600KB 的 agent 日志),
    题一多切页就卡;改成按题取,顺带把页面所需的字段都凑齐。
    """
    try:
        chain = parse_shared_timeline(rundir, entry)
    except Exception:
        chain = []
    try:
        findings = build_findings(rundir, entry)
    except Exception:
        findings = {"findings": [], "fails": [], "artifacts": [], "timeline_count": 0}
    try:
        graph = build_attack_graph(rundir, entry)
    except Exception as e:
        # 不能静默:否则"图是空的"这种表现根本查不出原因
        print(f"[dashboard] build_attack_graph 失败({entry.get('question_id')}): "
              f"{type(e).__name__}: {e}", flush=True)
        graph = None
    mode = graph_mode_for(entry, graph)
    if mode != "attack":
        graph = None            # 不是网络渗透题就别把链路数据发给前端
    return {
        "qid": entry.get("question_id", ""),
        "mode": mode,
        "chain": chain,
        "findings": findings,
        "graph": graph,
        "shared": shared_board(rundir, entry),
    }


def question_subs(rundir: Path, qid: str, limit: int = 30) -> list[dict]:
    """本题的提交记录(新的在前),给单题页用。"""
    sp = rundir / "logs" / "submissions.log"
    out: list[dict] = []
    if not sp.exists():
        return out
    try:
        for line in sp.read_text(encoding="utf-8", errors="ignore").splitlines()[-200:]:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("question_id") == qid:
                out.append(rec)
    except OSError:
        return out
    return list(reversed(out))[:limit]

# ---------------------------------------------------------------- 页面

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rotom 控制台</title>
<style>
/* ---- Rotom 控制台 · Hermes 风格(深色 dark-modern 为默认,浅色 light-modern 为 body.light) ----
   设计基因:零阴影(层级靠 fill+border)、零圆角、4px 网格、HarmonyOS Sans + Cascadia 等宽、
   主操作是中性黑白灰、状态只认三色(绿 ok / 黄 warn / 红 err)。 */
@font-face{font-family:"HarmonyOS Sans SC";font-weight:400;font-style:normal;font-display:swap;
  src:local("HarmonyOS Sans SC"),url("/assets/fonts/HarmonyOS_Sans_SC_Regular.woff2") format("woff2");}
@font-face{font-family:"HarmonyOS Sans SC";font-weight:700;font-style:normal;font-display:swap;
  src:local("HarmonyOS Sans SC Bold"),local("HarmonyOS Sans SC"),
  url("/assets/fonts/HarmonyOS_Sans_SC_Bold.woff2") format("woff2");}
:root{
  --bg:#111111; --surface:#181818; --surface-2:#202020; --surface-3:#2a2a2a;
  --border:#3a3a3a; --border-soft:#292929; --border-hover:#4a4a4a;
  --text:#f1f1ef; --text-body:#cececa; --text-meta:#a1a19d; --text-faint:#858581;
  --accent:#f1f1ef; --accent-contrast:#111111; --accent-soft:#2a2a2a; --accent-border:#858581;
  --accent-text:#f1f1ef;
  --success:#89d185; --success-bg:rgba(137,209,133,.10); --success-bd:rgba(137,209,133,.42);
  --error:#f48771; --error-bg:rgba(244,135,113,.10); --error-solid:#f48771;
  --warn:#cca700; --warn-bg:rgba(204,167,0,.12); --warn-bd:rgba(204,167,0,.42);
  --header-bg:rgba(24,24,24,.88); --hover:rgba(255,255,255,.05);
  --pill-bg:#2a2a2a; --code-bg:#151515;
  --radius:0; --radius-sm:0;
  --mono:"Cascadia Code","Cascadia Mono","JetBrains Mono","SF Mono",Menlo,Consolas,monospace;
  --font-ui:"HarmonyOS Sans SC","HarmonyOS Sans","PingFang SC","Segoe UI","Microsoft YaHei UI","Microsoft YaHei",-apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif;
  --pinhole:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'%3E%3Cpath d='M0 0H24M0 0V24' fill='none' stroke='%23f1f1ef' stroke-opacity='.05' stroke-width='1'/%3E%3Ccircle cx='0' cy='0' r='1.5' fill='%23f1f1ef' fill-opacity='.14'/%3E%3C/svg%3E");
  --ease:cubic-bezier(.2,0,0,1); --dur:120ms;
}
body.light{
  --bg:#f4f4f3; --surface:#ffffff; --surface-2:#f7f7f6; --surface-3:#ececeb;
  --border:#d5d5d2; --border-soft:#e8e8e5; --border-hover:#b8b8b4;
  --text:#171716; --text-body:#3f3f3d; --text-meta:#656562; --text-faint:#858581;
  --accent:#171716; --accent-contrast:#ffffff; --accent-soft:#ececeb; --accent-border:#777773;
  --accent-text:#171716;
  --success:#16825d; --success-bg:rgba(22,130,93,.10); --success-bd:rgba(22,130,93,.30);
  --error:#d13438; --error-bg:rgba(209,52,56,.10); --error-solid:#d13438;
  --warn:#9d6b00; --warn-bg:rgba(157,107,0,.10); --warn-bd:rgba(157,107,0,.30);
  --header-bg:rgba(244,244,243,.88); --hover:rgba(0,0,0,.04);
  --pill-bg:#ececeb; --code-bg:#f3f3f2;
  --pinhole:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'%3E%3Cpath d='M0 0H24M0 0V24' fill='none' stroke='%233f3f3c' stroke-opacity='.05' stroke-width='1'/%3E%3Ccircle cx='0' cy='0' r='1.5' fill='%233f3f3c' fill-opacity='.16'/%3E%3C/svg%3E");
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);background-image:var(--pinhole);color:var(--text-body);
  font:12.5px/1.75 var(--font-ui);letter-spacing:.1px;
  display:grid;grid-template-columns:248px minmax(0,1fr);overflow:hidden}
a{color:var(--accent-text);text-decoration:none}
a:hover{text-decoration:underline}
code,pre,.mono{font-family:var(--mono)}
:focus-visible{outline:2px solid var(--accent-border);outline-offset:-1px}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:var(--border-hover);border-radius:0}
::-webkit-scrollbar-track{background:transparent}

/* ---------------- 左栏:项目 + 任务列表(248px,Hermes sidebar) ---------------- */
aside{display:flex;flex-direction:column;min-height:0;background:var(--surface);border-right:1px solid var(--border-soft)}
.brand{display:flex;align-items:center;gap:8px;padding:14px 14px 10px;flex:0 0 auto}
.brand b{font-size:14px;color:var(--text);letter-spacing:.4px;font-weight:700}
.brand .spark{color:var(--accent-text)}
.projwrap{padding:0 12px 10px;flex:0 0 auto}
.projwrap select{width:100%;height:32px;background:var(--surface-2);color:var(--text-body);
  border:1px solid var(--border);border-radius:0;padding:6px 8px;font-size:12px;font-family:var(--font-ui)}
.searchwrap{padding:0 12px 10px;flex:0 0 auto;position:relative}
.searchwrap input{width:100%;height:32px;background:var(--surface-2);color:var(--text);
  border:1px solid var(--border);border-radius:0;padding:6px 8px 6px 26px;font-size:12px;font-family:var(--font-ui)}
.searchwrap:before{content:"⌕";position:absolute;left:21px;top:7px;color:var(--text-faint);font-size:13px}
.filters{display:flex;gap:4px;padding:0 12px 8px;flex:0 0 auto;flex-wrap:wrap}
.chip{background:var(--pill-bg);border:1px solid transparent;color:var(--text-meta);border-radius:0;
  padding:2px 9px;font-size:11px;cursor:pointer;user-select:none;transition:background-color var(--dur) var(--ease),border-color var(--dur) var(--ease)}
.chip:hover{border-color:var(--border-hover)}
.chip.active{background:var(--accent-soft);color:var(--accent-text);border-color:var(--accent-border)}
.chip b{font-weight:600;opacity:.75}
.tasks{flex:1 1 auto;overflow:auto;min-height:0;padding:0 8px 8px}
.task{display:flex;gap:8px;align-items:flex-start;padding:8px 9px;border-radius:0;
  cursor:pointer;border:1px solid transparent;border-left:2px solid transparent;
  transition:background-color var(--dur) var(--ease),border-color var(--dur) var(--ease)}
.task:hover{background:var(--hover)}
.task.sel{background:var(--accent-soft);border-color:var(--accent-border);border-left-color:var(--accent)}
.task .dot{width:8px;height:8px;border-radius:0;margin-top:6px;flex:0 0 auto;background:var(--text-faint)}
.task.run .dot{background:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);animation:pp 1.6s infinite}
.task.solved .dot{background:var(--success)}
.task.failed .dot{background:var(--error)}
@keyframes pp{50%{opacity:.35}}
.task .body{min-width:0;flex:1}
.task .t1{display:flex;justify-content:space-between;gap:6px;align-items:baseline}
.task .name{color:var(--text);font-size:12.5px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.task .flagprog{font-family:var(--mono);font-size:11px;color:var(--success);flex:0 0 auto}
.task .t2{display:flex;gap:6px;flex-wrap:wrap;color:var(--text-faint);font-size:11px;margin-top:1px}
.task .t2 .k{color:var(--text-meta)}
.sidefoot{flex:0 0 auto;border-top:1px solid var(--border-soft);padding:8px;display:flex;flex-direction:column;gap:4px}
.topbar .st{display:flex;align-items:baseline;gap:6px;font-size:11px;color:var(--text-meta)}
.topbar .st .lab{font-size:10.5px;color:var(--text-faint);letter-spacing:.4px}
.topbar .st b{font-family:var(--mono);font-size:12px;color:var(--text);font-weight:600}
.btns{display:flex;gap:8px;flex-wrap:wrap}
button{font-family:var(--font-ui);font-size:12px;height:28px;padding:0 12px;border-radius:0;border:1px solid var(--border);
  background:var(--surface-2);color:var(--text-body);cursor:pointer;
  transition:background-color var(--dur) var(--ease),border-color var(--dur) var(--ease),color var(--dur) var(--ease)}
button:hover{border-color:var(--border-hover);color:var(--text)}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-contrast);font-weight:600}
button.primary:hover{background:var(--accent);filter:brightness(1.12);color:var(--accent-contrast)}
button.danger{color:var(--error);border-color:var(--error-bd,rgba(244,135,113,.4))}
button.icon{height:28px;padding:0 8px}

/* ---------------- 主区:单题详情 ---------------- */
main{overflow:auto;min-height:0;padding:0 0 40px}
.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:12px;height:48px;
  padding:0 20px;background:var(--header-bg);backdrop-filter:blur(8px);border-bottom:1px solid var(--border-soft)}
.topbar .t{font-size:12px;color:var(--text-meta)}
.topbar .t b{color:var(--text);font-weight:600}
.grow{flex:1}
.wrap{padding:16px 20px 0;max-width:1120px}
.card{background:var(--surface);border:1px solid var(--border-soft);border-radius:0;margin-bottom:16px}
.card>h2{margin:0;padding:10px 14px;font-size:12.5px;font-weight:600;color:var(--text);
  border-bottom:1px solid var(--border-soft);display:flex;align-items:center;gap:8px;letter-spacing:.2px}
.card>h2 .sub{font-weight:400;color:var(--text-faint);font-size:11.5px}
.card>h2 .right{margin-left:auto;font-weight:400;color:var(--text-meta);font-size:11.5px}
.card .pad{padding:16px}
.pill{display:inline-flex;align-items:center;gap:5px;height:20px;padding:0 8px;border-radius:0;font-size:11px;
  background:var(--pill-bg);color:var(--text-meta)}
.pill.ok{background:var(--success-bg);color:var(--success);border:1px solid var(--success-bd)}
.pill.run{background:var(--accent-soft);color:var(--accent-text);border:1px solid var(--accent-border)}
.pill.warn{background:var(--warn-bg);color:var(--warn);border:1px solid var(--warn-bd)}
.pill.bad{background:var(--error-bg);color:var(--error);border:1px solid var(--error-bd)}
.empty{color:var(--text-faint);font-size:12px;padding:10px 0}

.qhead{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px}
.qhead h1{margin:0;font-size:16px;color:var(--text);font-weight:700}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:8px 18px;margin-top:12px}
.kv .row{display:flex;gap:7px;font-size:12px;min-width:0}
.kv .row .k{color:var(--text-faint);flex:0 0 auto}
.kv .row .v{color:var(--text-body);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* 长文 kv(结论/文件清单):默认口径是多列 auto-fit + 单行省略号,只适合短标签值(难度/分值);
   散文和清单套这一层 —— 一行一条、值换行,不然结论会被截成「确证新浪官方短链,落地j…」看不出内容。 */
.kv-wide{grid-template-columns:1fr;gap:7px}
.kv-wide .row{align-items:baseline}
.kv-wide .row .k{min-width:96px}
.kv-wide .row .v{white-space:normal;overflow:visible;text-overflow:clip;line-height:1.7;word-break:break-word}
.kv-wide .row .v button{margin:3px 6px 3px 0}
.desc{color:var(--text-body);font-size:12.5px;margin-top:10px;white-space:pre-wrap;line-height:1.75}
.bar{height:4px;border-radius:0;background:var(--surface-3);overflow:hidden;margin-top:10px}
.bar>i{display:block;height:100%;background:var(--accent)}

/* 攻击链路图 */
.atk{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px}
.atk .canvas{background:var(--code-bg);border:1px solid var(--border-soft);border-radius:0;overflow:auto}
.atk svg{display:block}
.atk .legend{display:flex;flex-wrap:wrap;gap:12px;padding:8px 2px 0;font-size:11px;color:var(--text-meta)}
.atk .legend i{display:inline-block;width:8px;height:8px;border-radius:0;margin-right:5px;vertical-align:-1px}
.nd{display:flex;gap:7px;padding:6px 8px;border-radius:0;cursor:pointer;
  transition:background-color var(--dur) var(--ease)}
.nd:hover{background:var(--hover)}
.nd.sel{background:var(--accent-soft);box-shadow:inset 2px 0 0 var(--accent)}
.nd .c{width:8px;height:8px;border-radius:0;flex:0 0 auto;margin-top:6px}
.nd .m{min-width:0}
.nd .n{color:var(--text);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nd .s{color:var(--text-faint);font-size:11px}
.node{cursor:pointer}
.node text{font:10.5px var(--mono);fill:var(--text-body);paint-order:stroke;stroke:var(--code-bg);stroke-width:2.5px;stroke-linejoin:round}
.node text.sub{font-size:9.5px;fill:var(--text-meta)}
.node.sel circle,.node.sel rect,.node.sel polygon{stroke:var(--accent);stroke-width:2.5}
.detailbox{margin-top:10px;background:var(--surface-2);border:1px solid var(--border-soft);border-radius:0;
  padding:10px 12px;font-size:12px}
.detailbox .h{color:var(--text);font-size:12px;margin-bottom:6px;display:flex;gap:8px;align-items:center;font-weight:600}
.detailbox pre{margin:6px 0 0;white-space:pre-wrap;word-break:break-all;color:var(--text-body);font-size:11.5px}

/* 解题链 */
.lanes{display:flex;flex-direction:column;gap:8px}
.lane{display:grid;grid-template-columns:52px minmax(0,1fr);gap:8px;align-items:start}
.lane .who{font-family:var(--mono);font-size:11px;color:var(--text-meta);padding-top:6px}
.ev{display:flex;gap:8px;align-items:baseline;padding:6px 9px;border-left:2px solid var(--border);
  background:var(--surface-2);border-radius:0;margin-bottom:5px;font-size:12px}
.ev .tm{font-family:var(--mono);font-size:11px;color:var(--text-faint);flex:0 0 auto}
.ev .tx{color:var(--text-body);min-width:0}
.ev.confirm{border-left-color:var(--success)}
.ev.plan{border-left-color:var(--accent)}
.ev.fail{border-left-color:var(--error)}
.ev.rescue{border-left-color:var(--warn)}
.ev.obs{opacity:.55}
/* 涉诈归档:树行内快捷归档/撤销(平时藏起来,悬停才露,别抢正文)+ 站点页归档卡的输入框 */
.ev .sz-act{margin-left:auto;flex:0 0 auto;opacity:0;height:19px;padding:0 7px;font-size:11px;
  border:1px solid var(--border-soft);background:var(--pill-bg);color:var(--text-meta)}
.ev:hover .sz-act,.ev .sz-act.show{opacity:1}
.ev .sz-act:hover{border-color:var(--border-hover);color:var(--text)}
.sz-arctag{margin-left:auto;flex:0 0 auto;font-size:10.5px;line-height:17px;padding:0 6px;
  color:var(--text-meta);background:var(--pill-bg);border:1px solid var(--border-soft)}
.sz-arctag + .sz-act{margin-left:0}          /* 徽标已经在右侧,按钮贴着它排 */
.sz-arcrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px}
.sz-arc-in{flex:1 1 300px;min-width:220px;height:28px;padding:0 8px;font-family:var(--font-ui);font-size:12px;
  background:var(--surface-2);color:var(--text);border:1px solid var(--border)}
.sz-arc-in:focus{outline:none;border-color:var(--accent-border)}

/* 收获 */
.fc{border:1px solid var(--border-soft);border-radius:0;padding:9px 12px;margin-bottom:8px;background:var(--surface-2)}
.fc .h{display:flex;gap:8px;align-items:center;font-size:11px;color:var(--text-faint);margin-bottom:3px}
.fc .b{font-size:12px;color:var(--text-body);white-space:pre-wrap}
.sev{display:inline-block;padding:1px 7px;border-radius:0;font-size:10.5px;font-weight:600;letter-spacing:.3px}
.sev.critical{background:var(--error-bg);color:var(--error);border:1px solid var(--error-bd)}
.sev.high{background:var(--warn-bg);color:var(--warn);border:1px solid var(--warn-bd)}
.sev.medium{background:var(--accent-soft);color:var(--accent-text);border:1px solid var(--accent-border)}
.sev.low,.sev.info{background:var(--pill-bg);color:var(--text-meta);border:1px solid var(--border-soft)}
.arts{display:flex;flex-wrap:wrap;gap:6px}
.arts .a{font-family:var(--mono);font-size:11px;background:var(--surface-2);border:1px solid var(--border-soft);
  border-radius:0;padding:3px 7px;color:var(--text-meta)}

/* agent 轨迹 */
.wk{border:1px solid var(--border-soft);border-radius:0;padding:10px 12px;margin-bottom:8px;background:var(--surface)}
.wk .wh{display:flex;gap:9px;align-items:center;font-size:11.5px;color:var(--text-meta);margin-bottom:5px;flex-wrap:wrap}
.act{font-family:var(--mono);font-size:11px;color:var(--text-body);padding:1px 0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.act.say{color:var(--accent-text)}
.act.err{color:var(--error)}
details{margin-top:6px}
details>summary{cursor:pointer;color:var(--text-meta);font-size:11.5px;user-select:none}
details pre{margin:8px 0 0;background:var(--code-bg);border:1px solid var(--border-soft);border-radius:0;
  padding:10px;max-height:340px;overflow:auto;font-size:11.5px;white-space:pre-wrap;word-break:break-word;color:var(--text-body)}

table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:var(--text-faint);font-weight:500;padding:5px 8px;border-bottom:1px solid var(--border);font-size:11px}
td{padding:6px 8px;border-bottom:1px solid var(--border-soft);color:var(--text-body);vertical-align:top}
td.mono{font-family:var(--mono);font-size:11px;word-break:break-all}

/* 抽屉 */
.drawer{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;z-index:60;justify-content:flex-end}
.drawer.open{display:flex}
.drawer .panel{width:min(760px,94vw);height:100%;background:var(--surface);border-left:1px solid var(--border);
  display:flex;flex-direction:column;min-height:0}
.drawer.wide .panel{width:min(1040px,96vw)}
.drawer .ph{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border-soft)}
.drawer .ph b{color:var(--text);font-size:13px}
.drawer .pb{flex:1;overflow:auto;min-height:0;padding:16px}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.f label{display:block;font-size:11.5px;color:var(--text-meta);margin-bottom:4px}
.f input,.f select{width:100%;height:32px;background:var(--surface-2);color:var(--text);border:1px solid var(--border);
  border-radius:0;padding:6px 8px;font-size:12px;font-family:var(--font-ui)}
.hint{font-size:11.5px;color:var(--text-faint);line-height:1.7;margin-top:9px}
.cfgseg{display:flex;align-items:center;gap:6px;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--border-soft)}
.cfgseg .chip{font-size:12px;padding:4px 10px}
.kbsplit{display:grid;grid-template-columns:230px minmax(0,1fr);gap:12px;height:100%}
.kblist .k{padding:5px 8px;border-radius:0;cursor:pointer;font-size:12px;color:var(--text-body);
  transition:background-color var(--dur) var(--ease),color var(--dur) var(--ease)}
.kblist .k:hover{background:var(--hover)}
.kblist .k.sel{background:var(--accent-soft);color:var(--accent-text);box-shadow:inset 2px 0 0 var(--accent)}
.kbview pre{background:var(--code-bg);border:1px solid var(--border-soft);border-radius:0;padding:12px;
  white-space:pre-wrap;word-break:break-word;font-size:11.5px;max-height:none;color:var(--text-body)}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--surface-3);
  border:1px solid var(--accent-border);color:var(--text);padding:8px 14px;border-radius:0;font-size:12px;
  z-index:99;opacity:0;transition:opacity var(--dur) var(--ease);pointer-events:none;max-width:80vw}
.toast.on{opacity:1}
@media(max-width:1000px){body{grid-template-columns:248px minmax(0,1fr)}.atk{grid-template-columns:minmax(0,1fr)}}

/* 侧栏底部按钮 = 等宽控制条(Hermes 排版:4px 网格、统一高度、主操作唯一) */
.sidefoot .btns{gap:4px}
.sidefoot .btns button{flex:1;min-width:0;padding:0 6px}
  .md-h { margin: .5em 0 .25em; }
  .md-p { margin: .3em 0; }
  .md-quote { border-left: 3px solid var(--border); margin: .4em 0; padding: .1em .9em; color: var(--text-meta); }
  .md-pre { background: rgba(127,127,127,.08); padding: 8px 10px; overflow: auto; font-size: 12px; }
  .md-tbl { overflow-x: auto; margin: .4em 0; }
  .md-tbl table { margin: 0; }
  .md-hr { border: none; border-top: 1px solid var(--border); margin: .6em 0; }
  /* ---- 思维导图(markmap 本体渲染,iframe 内嵌) ---- */
  .mm-svg { display:block; width:100%; height:560px; border:1px solid var(--border-soft); background:var(--surface); }
  .mm-tools { display:flex; gap:6px; align-items:center; margin-bottom:8px; }
  .mm-hint { padding:6px 2px 0; font-size:11px; color:var(--text-faint); }
  .sz-comp { border:1px solid var(--border-soft); background:var(--surface-2); margin:0 0 12px; }
  .sz-comp-head { display:flex; align-items:center; gap:10px; min-height:34px; padding:6px 10px; border-bottom:1px solid var(--border-soft); color:var(--text-meta); font-size:11px; }
  .sz-comp-head b { color:var(--text); font-size:12px; }
  .sz-comp-head .sz-comp-kinds { margin-left:auto; color:var(--text-faint); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .sz-comp-body { position:relative; overflow:auto; background:var(--code-bg); }
  .sz-comp-svg { position:absolute; inset:0; pointer-events:none; }
  .sz-comp-node { position:absolute; width:208px; height:52px; background:var(--surface); border:1px solid var(--border); border-left-width:3px; cursor:pointer; padding:6px 9px; overflow:hidden; z-index:1; }
  .sz-comp-node:hover { border-color:var(--border-hover); background:var(--surface-3); }
  .sz-comp-node b { display:block; font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:var(--text); }
  .sz-comp-node span { display:block; font-size:10.5px; color:var(--text-meta); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .sz-comp-node.sz-deep { border-left-color:var(--success); }
  .sz-comp-node.sz-mid { border-left-color:var(--warn); }
  .sz-comp-node.sz-legit { border-left-color:var(--error-solid); }
  .sz-comp-node.sz-surface, .sz-comp-node.sz-none { border-left-color:var(--border); }
  .sz-dot { display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:4px; background:var(--text-faint); }
  .sz-deep .sz-dot { background:var(--success); } .sz-mid .sz-dot { background:var(--warn); }
  .sz-legit .sz-dot { background:var(--error-solid); }
  .sz-graph-view { max-height:720px; overflow:auto; padding:2px; }
  .sz-rel-view { max-height:520px; overflow:auto; }
  .sz-rel-table { min-width:760px; }
  .sz-rel-table td { vertical-align:middle; }
  .sz-rel-dot { display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:5px; }
  .sz-tab { height:24px; padding:0 8px; font-size:11px; }
  .sz-tab.active { background:var(--accent); color:var(--accent-contrast); border-color:var(--accent); }
  .sz-legend { line-height:1.8; }
</style>
</head><body>
<script src="/assets/md.js?v=2"></script>
<script src="/assets/markmap/d3.js"></script>
<script src="/assets/markmap/markmap-lib.iife.js"></script>
<script src="/assets/markmap/markmap-view.js"></script>
<script src="/assets/markmap/render.js"></script>

<aside>
  <div class="brand"><span class="spark">⚡</span><b>Rotom</b>
    <span class="grow"></span>
    <button class="icon" onclick="toggleTheme()" title="切换主题">◐</button>
  </div>
  <div class="projwrap"><select id="proj" onchange="switchProject(this.value)"></select></div>
  <div class="searchwrap"><input id="q" placeholder="搜索题目 / 分类 / ID / 站点   (Ctrl+K)" oninput="renderTasks();if(curProject==='涉诈批次')renderSzTree();persistViewState()"></div>
  <div class="filters" id="filters"></div>
  <div class="tasks" id="tasks"><div class="empty">等待数据…</div></div>
  <div class="sidefoot">
    <div class="btns">
      <button class="primary" onclick="act('start')">▶ 开始跑</button>
      <button class="danger" onclick="act('stop')">■ 停止</button>
      <button onclick="act('restart')">⟲ 重启</button>
    </div>
    <div class="btns">
      <button onclick="openDrawer('settings')">⚙ 设置</button>
      <button onclick="openDrawer('records')">≡ 记录</button>
      <button onclick="openDrawer('kb')">◫ 知识库</button>
    </div>
  </div>
</aside>

<main>
  <div class="topbar">
    <span class="t" id="tb_proj">项目 <b>-</b></span>
    <span class="t" id="tb_run"></span>
    <span class="grow"></span>
    <span class="t st"><span class="lab">进度</span><b id="s_prog">-</b></span>
    <span class="t st" id="s_run_box"><span class="lab">并发</span><b id="s_run">-</b></span>
    <span class="t st" id="s_left_box"><span class="lab">剩余</span><b id="s_left">-</b></span>
    <span class="t st" id="s_ai_box" onclick="toggleAiPanel()" style="cursor:pointer" title="AI 调用用量与计时(点击展开)"><span class="lab">AI</span><b id="s_ai">-</b></span>
    <span class="t" id="tb_now"></span>
  </div>
  <div id="view"><div class="wrap"><div class="card"><div class="pad"><div class="empty">从左侧选一道题查看它的完整解题过程</div></div></div></div></div>
</main>
<div id="ai_panel" style="display:none;position:fixed;right:14px;top:56px;z-index:80;width:460px;max-height:72vh;overflow:auto;background:var(--bg-card,#16181d);border:1px solid var(--line,#2a2e37);border-radius:8px;padding:12px;font-size:12px;box-shadow:0 8px 28px rgba(0,0,0,.35)"></div>

<div class="drawer" id="d_settings"><div class="panel">
  <div class="ph"><b>设置</b><span class="grow"></span>
    <span class="t" id="msg_cfg" style="font-size:11.5px;color:var(--text-meta)"></span>
    <button onclick="closeDrawer()">关闭</button></div>
  <div class="pb">
    <div class="cfgseg">
      <button class="chip active" id="cfg_t_basic" onclick="cfgView('basic')">常用</button>
      <button class="chip" id="cfg_t_adv" onclick="cfgView('adv')">高级(运行参数)</button>
      <span class="grow"></span>
      <button class="primary" onclick="saveConfig()">保存配置</button>
    </div>
    <div id="cfg_basic">
    <div class="card"><h2>跑分平台 <span class="sub">众测:容器自开自关 + 一题多 flag</span></h2><div class="pad">
      <div class="form">
        <div class="f"><label>平台适配器</label><select id="f_CONTEST_ADAPTER" onchange="dirty=true">
          <option value="tsec">tsec · TSec Benchmark</option><option value="arena">arena · NSSCTF 靶场</option>
          <option value="newrank">newrank · 2026 比武新榜单</option>
          <option value="">(留空)· 原 CTF 平台</option></select></div>
        <div class="f"><label>BENCHMARK_BASE_URL</label><input id="f_BENCHMARK_BASE_URL" oninput="dirty=true" placeholder="创建跑分任务时下发"></div>
        <div class="f"><label>BENCHMARK_TOKEN</label><input id="f_BENCHMARK_TOKEN" oninput="dirty=true" placeholder="UUID"></div>
        <div class="f"><label>VPN 预检地址</label><input id="f_VPN_CHECK_URL" oninput="dirty=true" placeholder="http://10.0.100.58"></div>
      </div>
      <div class="btns" style="margin-top:12px">
        <button onclick="act('test-vpn')">测试 VPN</button>
        <button onclick="act('test-contest')">测试平台接口</button>
        <span class="t" id="msg_platform" style="font-size:11.5px;color:var(--text-meta)"></span>
      </div>
      <div class="hint">题目入口在靶场 VPN 内:VPN 没连上时 harness 不会查题、不会派 agent(连上自动开跑)。</div>
    </div></div>
    <div class="card"><h2>模型</h2><div class="pad">
      <div class="form">
        <div class="f"><label>Provider 档案(存过的配置,选中直接应用)</label>
          <div style="display:flex;gap:8px">
            <select id="f_saved_prov" style="flex:1" onchange="provFillForm()"></select>
            <button onclick="provApply()">应用</button>
            <button onclick="provSave()">存当前</button>
            <button onclick="provDelete()">删</button>
          </div>
          <span class="t" id="msg_prov" style="font-size:11.5px;color:var(--text-meta)"></span></div>
        <div class="f"><label>Base URL</label><input id="f_PI_BASE_URL" oninput="dirty=true"></div>
        <div class="f"><label>API Key</label><input id="f_PI_API_KEY" oninput="dirty=true"></div>
        <div class="f"><label>模型名</label><input id="f_PI_MODEL" oninput="dirty=true"></div>
        <div class="f"><label>API 类型</label><select id="f_PI_API_TYPE" onchange="dirty=true">
          <option value="openai-completions">openai-completions</option>
          <option value="anthropic-messages">anthropic-messages</option>
          <option value="openai-responses">openai-responses</option>
          <option value="google-generative-ai">google-generative-ai</option></select></div>
      </div>
      <div class="btns" style="margin-top:12px"><button onclick="act('test')">测试模型</button>
        <span class="t" id="msg_model" style="font-size:11.5px;color:var(--text-meta)"></span></div>
    </div></div>
    <div class="card"><h2>项目</h2><div class="pad">
      <div class="form">
        <div class="f"><label>全局默认策略包(默认项目 / 选"跟随全局"的新项目用它)</label><select id="f_PROFILE" onchange="dirty=true">
          <option value="zhongce">zhongce · 众测/跑分(容器自管 + 多 flag)</option>
          <option value="ctf">ctf · CTF 比赛(平台托管容器)</option>
          <option value="pentest">pentest · 正式渗透(目标来自 targets.json,产出报告)</option>
          <option value="shezha">shezha · 涉诈网站侦查(自动批次,代理合规闸门)</option></select></div>
        <div class="f"><label>新建项目(独立工作区与状态)</label>
          <input id="newproj" placeholder="例如 round2、练习-多题" onkeydown="if(event.key==='Enter')createProject()"></div>
        <div class="f"><label>策略包(建项目时定,之后按项目生效)</label><select id="newproj_profile">
          <option value="">跟随全局(.env 里的策略包)</option>
          <option value="zhongce">zhongce · 众测/跑分(容器自管+多 flag)</option>
          <option value="ctf">ctf · CTF 比赛(平台托管容器)</option>
          <option value="pentest">pentest · 正式渗透(目标来自 targets.json)</option>
          <option value="shezha">shezha · 涉诈网站侦查(自动批次,代理合规闸门)</option></select></div>
      </div>
      <div class="btns" style="margin-top:12px"><button onclick="createProject()">创建项目</button>
        <button onclick="patrolNow()">总Agent巡检当前项目</button>
        <span class="t" id="msg_patrol" style="font-size:11.5px;color:var(--text-meta)"></span>
        <span class="t" id="msg_proj" style="font-size:11.5px;color:var(--text-meta)"></span></div>
      <div class="hint" style="margin-top:10px">已有项目 · 删除会连同该项目的 work / logs / board 一起删掉(不可恢复):</div>
      <div id="projlist"></div>
      <div id="patrol_out" class="hint" style="max-height:280px;overflow:auto;margin-top:8px"></div>
    </div></div>
    <div class="card"><h2>涉诈自动批次</h2><div class="pad">
      <div id="sz_setup">
        <div class="form">
          <div class="f"><label>作战仓库根目录(…/渗透/涉诈)</label><input id="sz_root" placeholder="填涉诈项目根目录,绑一次就行"></div>
          <div class="f"><label>模型 provider(留空 = bigmodel-shezha)</label><input id="sz_pi_provider" placeholder="bigmodel-shezha"></div>
          <div class="f"><label>模型名(留空 = GLM-5.3-Flash)</label><input id="sz_pi_model" placeholder="GLM-5.3-Flash"></div>
        </div>
        <div class="btns" style="margin-top:12px"><button onclick="szConfig()">绑定作战仓库</button>
          <span class="t" id="sz_msg" style="font-size:11.5px;color:var(--text-meta)"></span></div>
      </div>
      <div id="sz_main" hidden>
        <div class="form">
          <div class="f"><label>目标榜单 Excel(可多选)</label><input type="file" id="sz_files" multiple accept=".xlsx,.xlsm,.xls"></div>
          <div class="f"><label>批次大小(每批目标数)</label><input id="sz_bs" value="10"></div>
          <div class="f"><label>批次号(留空 = 自动取历史最大+1)</label><input id="sz_bn" placeholder="自动"></div>
          <div class="f"><label>新档落的来源库</label><input id="sz_lib" value="其他来源_未入四表"></div>
          <div class="f"><label>重打子串(续打/重打已建档目标,留空 = 忽略已建档)</label><input id="sz_redo" placeholder="例如 gsm888"></div>
        </div>
        <div class="btns" style="margin-top:12px">
          <button onclick="szUpload()">① 上传 Excel · 切批次</button>
          <button onclick="szRunner('start')">② 开批次</button>
          <button onclick="szRunner('stop')">停止</button>
          <button onclick="szClose(false)">③ 收工(总表+对账+审计)</button>
          <button onclick="szClose(true)">③ 收工+书记员写汇总</button>
          <span class="t" id="sz_msg2" style="font-size:11.5px;color:var(--text-meta)"></span></div>
        <div class="hint" id="sz_summary" style="margin-top:10px"></div>
        <div class="hint" id="sz_close_out" style="white-space:pre-wrap;max-height:180px;overflow:auto"></div>
        <div class="btns" style="margin-top:8px">
          <button onclick="szCluster()">AI 归簇(增量)</button>
          <button onclick="szReview()">AI 读报告·审成果</button>
          <button onclick="szTimeline(7)">近7天时间线</button>
          <button onclick="szTimeline(30)">近30天</button>
          <button onclick="szWeekly()">AI 周报</button>
          <span class="t" id="sz_msg4" style="font-size:11.5px;color:var(--text-meta)"></span></div>
        <div class="hint" id="sz_sync" style="margin-top:8px"></div>
        <div class="btns" style="margin-top:4px">
          <button onclick="szSyncNow()">立即同步(拉取仓库新料)</button>
          <button onclick="szSyncToggle()">开/关 自动同步</button>
          <span class="t" id="sz_msg5" style="font-size:11.5px;color:var(--text-meta)"></span></div>
        <div id="sz_tl" class="hint" style="white-space:pre-wrap;max-height:220px;overflow:auto;margin-top:6px">点「近7天时间线」查看自动攒的探测时间线(零 AI,直接来自 findings/批次/车道/巡检)。</div>
        <div id="sz_targets" style="margin-top:8px"></div>
        <div class="hint" style="margin-top:6px">绑定的是 <span id="sz_root_cur"></span>;改绑定/换模型在
          <a href="#" onclick="szReconfig();return false">这里展开设置</a>。</div>
        <div id="sz_setup2" hidden>
          <div class="form" style="margin-top:8px">
            <div class="f"><label>作战仓库根目录</label><input id="sz_root2"></div>
            <div class="f"><label>模型 provider(pi 已有的直接选)</label><input id="sz_pi_provider2" list="pi_prov_list" onchange="renderProv()"></div>
            <div class="f"><label>模型名</label><input id="sz_pi_model2" list="pi_model_list"></div>
            <datalist id="pi_prov_list"></datalist>
            <datalist id="pi_model_list"></datalist>
          </div>
          <div class="btns" style="margin-top:8px"><button onclick="szConfig2()">保存</button>
            <span class="t" id="sz_msg3" style="font-size:11.5px;color:var(--text-meta)"></span></div>
        </div>
      </div>
    </div></div>
  </div>
    <div id="cfg_adv" hidden>
    <div class="card"><h2>运行参数</h2><div class="pad">
      <div class="form">

        <div class="f"><label>起始并发</label><input id="f_START_WORKERS" oninput="dirty=true"></div>
        <div class="f"><label>容器名额(平台硬上限 3)</label><input id="f_MAX_ACTIVE_CHALLENGES" oninput="dirty=true"></div>
        <div class="f"><label>单题时间片(秒,用满无进展就轮换)</label><input id="f_QUESTION_SLICE_SECONDS" oninput="dirty=true"></div>
        <div class="f"><label>单题单次限时(秒)</label><input id="f_WORKER_TIMEOUT" oninput="dirty=true"></div>
        <div class="f"><label>新 agent 保护期(秒,0=关)</label><input id="f_WORKER_MIN_LIFETIME" oninput="dirty=true"></div>
        <div class="f"><label>重启时在跑的 agent</label><select id="f_ADOPT_WORKERS" onchange="dirty=true">
          <option value="1">1 · 接管,不打断(推荐)</option><option value="0">0 · 全部杀掉重来</option></select></div>
        <div class="f"><label>轮换遇到在跑的 agent</label><select id="f_ROTATE_WAIT_RUNNING" onchange="dirty=true">
          <option value="1">1 · 等它跑完再让位(推荐)</option><option value="0">0 · 时间片到就杀掉让位</option></select></div>
        <div class="f"><label>总时长(分钟,0=不限)</label><input id="f_ROUND_WINDOW_MINUTES" oninput="dirty=true"></div>
        <div class="f"><label>harness 代取 hint:失败几次后自动取(0=默认·agent 自主用 get_hint.py)</label><input id="f_HINT_AFTER_ATTEMPTS" oninput="dirty=true"></div>
        <div class="f"><label>自动注入:首发即取的难度(逗号分隔;自动注入默认关)</label><input id="f_HINT_EARLY_DIFFS" oninput="dirty=true" placeholder="hard,medium"></div>
        <div class="f"><label>永不取 hint 的难度(逗号分隔)</label><input id="f_HINT_SKIP_DIFFS" oninput="dirty=true" placeholder="easy"></div>
        <div class="f"><label>平台接口连续失败重报间隔(秒)</label><input id="f_API_FAIL_RELOG_S" oninput="dirty=true" placeholder="600"></div>
        <div class="f"><label>调度停滞告警阈值(秒;0=关)</label><input id="f_STALL_WARN_S" oninput="dirty=true" placeholder="900"></div>
        <div class="f"><label>停滞判定的最少未解题数</label><input id="f_STALL_MIN_PENDING" oninput="dirty=true" placeholder="10"></div>
        <div class="f"><label>模式</label><select id="f_IGNORE_SOLVED" onchange="dirty=true">
          <option value="0">0 · 正式</option><option value="1">1 · 练习(重跑已通关)</option></select></div>
        <div class="f"><label>网络</label><select id="f_NET_PROXY" onchange="dirty=true">
          <option value="direct">direct · 直连(推荐)</option><option value="system">system · 走系统代理</option></select></div>
      </div>
      <div class="btns" style="margin-top:12px"><button class="primary" onclick="saveConfig()">保存配置</button>
        <span class="hint">保存后写入 .env 并同步到 pi provider;平台/token/模型改动需点「重启」才生效。</span></div>
    </div></div>
    </div>
  </div>
</div></div>

<div class="drawer wide" id="d_records"><div class="panel">
  <div class="ph"><b>记录</b><span class="grow"></span><button onclick="closeDrawer()">关闭</button></div>
  <div class="pb">
    <div class="card"><h2>提交记录 <span class="right" id="subs_n"></span></h2><div class="pad" id="subs"></div></div>
    <div class="card"><h2>事件流</h2><div class="pad" id="facts"></div></div>
  </div>
</div></div>

<div class="drawer wide" id="d_kb"><div class="panel">
  <div class="ph"><b>知识库</b><span class="grow"></span>
    <input id="kb_q" placeholder="检索手法 / payload(支持中英文)" style="width:260px;background:var(--surface-2);
      color:var(--text);border:1px solid var(--border);border-radius:6px;padding:5px 8px;font-size:12px"
      onkeydown="if(event.key==='Enter')kbSearch()">
    <button onclick="kbSearch()">检索</button><button onclick="closeDrawer()">关闭</button></div>
  <div class="pb"><div class="kbsplit">
    <div class="kblist" id="kb_list"></div>
    <div class="kbview"><div id="kb_results"></div><pre id="kb_view" style="display:none"></pre></div>
  </div></div>
</div></div>

<div class="toast" id="toast"></div>
<script>
/* =========================================================================
   通用 agent 式控制台:左栏 = 项目 + 任务列表,主区 = 选中任务的完整解题过程。
   每 2s 轮询 /api/state 只刷"左栏 + 顶栏";单题详情按需拉 /api/question,
   并且**内容没变就不重写 DOM**(否则轮询会把用户的滚动位置和展开状态冲掉 —— 实测 bug)。
   ========================================================================= */
const esc = s => (s ?? '').toString().replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const FIELDS = ['PROFILE','CONTEST_ADAPTER','BENCHMARK_BASE_URL','BENCHMARK_TOKEN','VPN_CHECK_URL',
                'PI_BASE_URL','PI_API_KEY','PI_MODEL','PI_API_TYPE',
                'START_WORKERS','MAX_ACTIVE_CHALLENGES','QUESTION_SLICE_SECONDS','WORKER_TIMEOUT',
                'WORKER_MIN_LIFETIME','ADOPT_WORKERS','ROTATE_WAIT_RUNNING',
                'ROUND_WINDOW_MINUTES','HINT_AFTER_ATTEMPTS','HINT_EARLY_DIFFS','HINT_SKIP_DIFFS',
                'API_FAIL_RELOG_S','STALL_WARN_S','STALL_MIN_PENDING','IGNORE_SOLVED','NET_PROXY'];
const STATUS = {pending:['待派发',''], running:['运行中','run'], solved:['已解出','ok'], failed:['已放弃','bad']};
const COL_X = {target:110, endpoint:310, tool:520, script:520, finding:720, goal:920};
const ROW_H = 46, PAD_T = 46, PAD_B = 34, W = 1020;
const NCOLOR = {target:'#f1f1ef', endpoint:'#b8b8b4', tool:'#858581', script:'#dcdcaa', finding:'#f48771', goal:'#89d185'};
const SEV_R = {critical:14, high:12, medium:10, low:9, info:9};
const SEV_ORDER = ['critical','high','medium','low','info'];

let state = null, curProject = localStorage.getItem('rotom_project') || '默认项目';
let sel = null, qdata = null, filter = 'all', dirty = false, qFetchedAt = 0, qHash = '';
let selNode = null;   // 当前点开的图节点,重建后要复原(否则轮询一刷,详情就没了)
const $ = id => document.getElementById(id);

function hash(s){ let h = 5381; for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0; return h; }
/* 只在内容变化时重写 DOM,并保住 <details> 展开状态与各处滚动位置(修"下拉后弹回顶部") */
function setHtml(el, html){
  if (!el) return false;
  const h = hash(html);
  if (el.dataset.h === String(h)) return false;
  const dOpen = [...el.querySelectorAll('details')].map(d => d.open);
  const sc = [...el.querySelectorAll('pre,.scroll')].map(e => e.scrollTop);
  el.dataset.h = String(h);
  el.innerHTML = html;
  [...el.querySelectorAll('details')].forEach((d, i) => { if (dOpen[i]) d.open = true; });
  [...el.querySelectorAll('pre,.scroll')].forEach((e, i) => { if (sc[i]) e.scrollTop = sc[i]; });
  return true;
}
function toast(msg, bad){
  const t = $('toast'); t.textContent = msg; t.className = 'toast on' + (bad ? ' err' : '');
  setTimeout(() => { t.className = 'toast'; }, 3200);
}
async function get(url){ const r = await fetch(url); return r.json(); }
async function post(url, body){
  const r = await fetch(url, {method:'POST', body: JSON.stringify(body || {})});
  return r.json();
}
function toggleTheme(){
  document.body.classList.toggle('light');
  localStorage.setItem('rotom_theme', document.body.classList.contains('light') ? 'light' : 'dark');
}
if (localStorage.getItem('rotom_theme') === 'light') document.body.classList.add('light');

/* ---------------- 抽屉 ---------------- */
function openDrawer(which){
  $('d_' + which).classList.add('open');
  if (which === 'settings'){ fillConfig(); if (szState) renderSz(); }
  if (which === 'kb') loadKbList();
  if (which === 'records') renderRecords();
}
function cfgView(v){
  const b = v === 'basic';
  // 用 hidden 属性(而不是只改 style.display):[hidden] 的 UA 规则不吃 display:'' 的空值,
  // 只写 style 会"看起来没切"(实测踩过)
  $('cfg_basic').hidden = !b;
  $('cfg_adv').hidden = b;
  $('cfg_t_basic').classList.toggle('active', b);
  $('cfg_t_adv').classList.toggle('active', !b);
}
function closeDrawer(){ document.querySelectorAll('.drawer').forEach(d => d.classList.remove('open')); persistViewState(); }
document.querySelectorAll('.drawer').forEach(d => d.addEventListener('click', e => { if (e.target === d) closeDrawer(); }));

/* ---------------- 顶栏 + 左栏 ---------------- */
function matches(q){
  const kw = ($('q').value || '').trim().toLowerCase();
  if (kw && !(q.title + ' ' + q.category + ' ' + q.qid + ' ' + (q.workdir || '')).toLowerCase().includes(kw)) return false;
  if (filter === 'all') return true;
  if (filter === 'running') return q.status === 'running';
  if (filter === 'pending') return q.status === 'pending';
  return q.status === filter;
}
function taskRow(q){
  const st = STATUS[q.status] || [q.status, ''];
  const cls = 'task' + (q.status === 'running' ? ' run' : q.status === 'solved' ? ' solved' : q.status === 'failed' ? ' failed' : '')
                 + (sel === q.qid ? ' sel' : '');
  const meta = [esc(q.category || '-'), (q.score || 0) + '分'];
  if (q.elapsed != null) meta.push('已跑 ' + fmtSec(q.elapsed));
  else if (q.attempts) meta.push('尝试 ' + q.attempts + ' 次');
  if (q.workers && q.workers.length) meta.push('agent ' + q.workers.length);
  if (q.container_ready) meta.push('<span style="color:var(--success)">容器已开</span>');
  else if (q.container_status) meta.push('容器 ' + esc(q.container_status));
  return `<div class="${cls}" onclick="pick('${esc(q.qid)}')">
    <span class="dot"></span>
    <span class="body">
      <span class="t1"><span class="name">${esc(q.title || q.qid)}</span>
        ${q.flag_progress ? `<span class="flagprog">${esc(q.flag_progress)}</span>` : ''}</span>
      <span class="t2">${meta.join(' · ')}</span>
    </span></div>`;
}
function renderTasks(){
  if (!state) return;
  const isSz = state.project === '涉诈批次';
  if (isSz){ szLoadTree(); renderSzTree(); return; }   // 涉诈:左栏 = 簇→站点树(整库,不限本批)
  const cont = $('tasks');
  const qs = state.questions.filter(matches);
  const counts = {all: state.questions.length};
  for (const k of ['running','pending','solved','failed']) counts[k] = state.questions.filter(q => q.status === k).length;
  const labels = isSz ? [['all','全部'],['running','探测中'],['pending','待派发'],['solved','已交付'],['failed','已放弃']]
                      : [['all','全部'],['running','运行中'],['pending','待派发'],['solved','已解出'],['failed','已放弃']];
  setHtml($('filters'),
    labels
      .map(([k, label]) => `<span class="chip${filter === k ? ' active' : ''}" onclick="setFilter('${k}')">${label} <b>${counts[k] || 0}</b></span>`)
      .join(''));
  if (!qs.length){
    const kw = (($('q')||{}).value || '').trim();
    cont.innerHTML = '<div class="empty">' + (kw
      ? '没有匹配 “' + esc(kw) + '” 的任务 —— 搜索框里还留着关键词, <a href="#" onclick="clearSearch();return false" style="text-decoration:underline">点我清除</a>'
      : '没有匹配的任务') + '</div>';
    cont.dataset.h = ''; return;
  }
  const html = qs.map(taskRow).join('');
  // 列表内容变了(选中态/状态/进度都会变),用 setHtml 会频繁重写;列表本身不滚动时无副作用
  const key = hash(html);
  if (cont.dataset.h !== String(key)){ cont.dataset.h = String(key); cont.innerHTML = html; }
}
function clearSearch(){
  if ($('q')) $('q').value = '';
  renderTasks();                                        // CTF 任务列表(涉诈项目里它内部转 renderSzTree)
  if (curProject === '涉诈批次') renderSzTree();         // 树只在涉诈视图重渲,别去动 CTF 的列表容器
  persistViewState();
}
function setFilter(f){ filter = f; renderTasks(); persistViewState(); }
function fmtSec(s){ s = Math.max(0, Math.round(s)); return s < 60 ? s + 's' : Math.floor(s/60) + 'm' + String(s%60).padStart(2,'0') + 's'; }
function fmtLeft(s){ if (s == null || s < 0) return '不限'; const m = Math.floor(s/60); return m + ' 分'; }
function switchProject(name){
  localStorage.setItem('rotom_project', name);
  if (name === curProject) return;
  curProject = name; sel = null; qdata = null; qHash = '';
  $('view').innerHTML = '<div class="wrap"><div class="card"><div class="pad"><div class="empty">从左侧选一道题查看它的完整解题过程</div></div></div></div>';
  viewRestored = false; persistViewState(); tick();
}
async function createProject(){
  const name = ($('newproj').value || '').trim();
  if (!name){ $('msg_proj').textContent = '请先填项目名'; return; }
  const profile = ($('newproj_profile') || {}).value || '';
  const r = await post('/api/projects', {action:'create', name, profile});
  $('msg_proj').textContent = r.message;
  $('newproj').value = '';
  renderProjects(); renderProjectList();
  tick();
}
async function deleteProject(name){
  if (!confirm('删除项目「' + name + '」?会连同它的 work / logs / board 一起删除,不可恢复。' + '(想保留就把它停下来放着)')) return;
  const r = await post('/api/projects', {action:'delete', name});
  $('msg_proj').textContent = r.message;
  toast(r.message, !r.ok);
  renderProjects(); renderProjectList();
  tick();
}
function renderProjectList(){
  if (!state) return;
  const rows = (state.projects || []).map(p => {
    const cur = p.name === state.project;
    const prof = p.profile ? esc(p.profile) : '<span style="color:var(--text-meta)">跟随全局</span>';
    const del = p.builtin ? '' :
      `<button onclick="deleteProject('${esc(p.name)}')" style="margin-left:8px;padding:2px 8px;font-size:11px">删除</button>`;
    return `<div class="ev obs" style="border-left-color:var(--border)">
      <span class="tx"><b>${esc(p.name)}</b>${cur ? ' <span style="color:var(--success)">· 当前</span>' : ''}
        <span style="color:var(--text-meta)"> · ${p.questions} 题 · 策略包 ${prof}</span>${del}</span></div>`;
  }).join('');
  setHtml($('projlist'), rows || '<div class="empty">还没有项目</div>');
}
function renderProjects(){
  if (!state) return;
  const sel_ = $('proj');
  const html = state.projects.map(p => `<option value="${esc(p.name)}">${esc(p.name)} · ${p.questions} 题</option>`).join('');
  if (sel_.dataset.h !== String(hash(html))){ sel_.dataset.h = String(hash(html)); sel_.innerHTML = html; }
  sel_.value = state.project;
  curProject = state.project;
  const cur = (state.projects || []).find(p => p.name === state.project) || {};
  const prof = cur.profile ? ` · ${cur.profile}` : '';
  $('tb_proj').innerHTML = `项目 <b>${esc(state.project)}</b>${esc(prof)}`;
}
function renderHeader(){
  if (!state) return;
  const isSz = state.project === '涉诈批次';
  const unit = isSz ? ' 站点' : ' 题';
  const res = state.residents;
  let resTxt = '';
  if (res && res.running != null){
    resTxt = ` · 常驻 ${res.running} 路${res.zombie ? `(⚠${res.zombie} 僵尸)` : ''}`;
  }
  // 涉诈场景:并发/剩余(跑分概念)没有意义,隐藏;进度行换成 批次·交付·findings·常驻
  $('s_run_box').style.display = isSz ? 'none' : '';
  $('s_left_box').style.display = isSz ? 'none' : '';
  $('s_prog').textContent = isSz
    ? `批次 ${szState?.batch ?? '?'} · 交付 ${state.solved}/${state.total} · findings ${state.findings ?? 0}${resTxt}`
    : `${state.solved}/${state.total}${unit} · flag ${state.flags ?? 0}/${state.total_flags ?? 0}`;
  $('s_run').textContent = `${state.running_workers} / ${state.limit ?? '-'}`;
  $('s_left').textContent = fmtLeft(state.time_left);
  const r = state.runner || {};
  $('tb_run').innerHTML = r.running
    ? `<span class="pill run">runner 运行中</span>${state.paused ? ' <span class="pill warn">额度暂停</span>' : ''}`
    : `<span class="pill">runner 未运行</span>`;
  $('tb_now').textContent = state.now || '';
}

/* ---------------- AI 用量/计时 ---------------- */
let aiUsage = null, aiFetchedAt = 0;
function fmtTok(n){
  n = Number(n) || 0;
  if (n >= 1e9) return (n/1e9).toFixed(2) + 'G';
  if (n >= 1e6) return (n/1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1) + 'k';
  return String(n);
}
async function loadAiUsage(force){
  const now = Date.now();
  if (!force && now - aiFetchedAt < 4000) return;
  aiFetchedAt = now;
  try { aiUsage = await get(`/api/ai_usage?project=${encodeURIComponent(curProject)}`); }
  catch (e){ return; }
  renderAiBadge();
}
function renderAiBadge(){
  const el = $('s_ai'); if (!el) return;
  if (!aiUsage || !aiUsage.available){ el.textContent = '—'; return; }
  const t = aiUsage.totals || {}, r = aiUsage.rates || {}, l = aiUsage.latency || {};
  el.textContent = `${fmtTok(t.total_tokens)} tok · ${fmtTok(r.tokens_per_min)}/min · ${((Number(l.avg_ms)||0)/1000).toFixed(1)}s`;
  el.title = `AI 调用监控:${t.requests||0} 次请求 / ${aiUsage.active_workers||0} 个活跃 worker(点击展开)`;
  const p = $('ai_panel');
  if (p && p.style.display !== 'none') renderAiPanel();
}
function toggleAiPanel(){
  const p = $('ai_panel'); if (!p) return;
  if (p.style.display === 'none'){ p.style.display = ''; loadAiUsage(true); renderAiPanel(); }
  else p.style.display = 'none';
}
function renderAiPanel(){
  const p = $('ai_panel'); if (!p) return;
  if (!aiUsage || !aiUsage.available){
    p.innerHTML = '<div class="empty">还没有 AI 用量数据(先开跑,监控每 5 秒落一次 logs/ai_usage.json)</div>';
    return;
  }
  const t = aiUsage.totals || {}, r = aiUsage.rates || {}, l = aiUsage.latency || {};
  const rows = Object.entries(aiUsage.by_question || {})
    .sort((a,b) => (Number(b[1].total_tokens)||0) - (Number(a[1].total_tokens)||0)).slice(0, 12)
    .map(([qid,x]) => `<tr><td>${esc(qid)}</td><td>${x.requests||0}</td><td>${fmtTok(x.total_tokens)}</td><td>${((Number(x.avg_latency_ms)||0)/1000).toFixed(1)}s</td><td>${x.elapsed_s||0}s</td></tr>`).join('');
  p.innerHTML = `<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <b style="font-size:13px">AI 调用监控</b><span class="grow"></span>
      <span class="t" style="font-size:11px">${esc(aiUsage.generated_at_local||'')}</span>
      <button onclick="toggleAiPanel()">×</button></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 14px;margin-bottom:10px">
      <div>请求数 <b>${t.requests||0}</b> · 错误 <b>${t.errors||0}</b></div>
      <div>活跃 worker <b>${aiUsage.active_workers||0}</b></div>
      <div>输入 <b>${fmtTok(t.input_tokens)}</b> · 输出 <b>${fmtTok(t.output_tokens)}</b></div>
      <div>缓存读 <b>${fmtTok(t.cache_read_tokens)}</b> · 写 <b>${fmtTok(t.cache_write_tokens)}</b></div>
      <div>总 token <b>${fmtTok(t.total_tokens)}</b>(<b>${fmtTok(r.tokens_per_min)}</b>/min)</div>
      <div>成本 <b>${(Number(t.cost_total)||0).toFixed(4)}</b></div>
      <div>平均延迟 <b>${((Number(l.avg_ms)||0)/1000).toFixed(2)}s</b></div>
      <div>P50 <b>${((Number(l.p50_ms)||0)/1000).toFixed(2)}s</b> · P95 <b>${((Number(l.p95_ms)||0)/1000).toFixed(2)}s</b></div>
      <div>耗时 <b>${Math.round((Number(aiUsage.elapsed_s)||0)/60)} 分钟</b></div>
      <div>请求速率 <b>${(Number(r.requests_per_min)||0).toFixed(1)}</b>/min</div>
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:11.5px">
      <thead><tr style="color:var(--text-meta)"><th align="left">题目</th><th align="left">请求</th><th align="left">token</th><th align="left">平均延迟</th><th align="left">耗时</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5" class="empty">暂无</td></tr>'}</tbody></table>`;
}

/* ---------------- 单题详情 ---------------- */
async function pick(qid){
  sel = qid; qdata = null; qHash = ''; qFetchedAt = 0; selNode = null;
  renderTasks();
  persistViewState();          // 选中的题要进 URL,否则刷新后回到空详情(实测漏了这行)
  await loadQuestion(true);
}
async function loadQuestion(force){
  if (!sel) return;
  const now = Date.now();
  const running = state && state.questions.find(q => q.qid === sel && q.status === 'running');
  if (!force && !running && qdata) return;
  if (!force && qdata && now - qFetchedAt < 4000) return;
  qFetchedAt = now;
  try {
    qdata = await get(`/api/question?project=${encodeURIComponent(curProject)}&qid=${encodeURIComponent(sel)}`);
  } catch (e){ toast('题目数据加载失败:' + e, true); return; }
  renderTask();
}
function qmeta(){
  const q = state.questions.find(x => x.qid === sel) || {};
  return Object.assign({}, q, (qdata && qdata.question) || {});
}
function renderTask(){
  if (!sel || !state){ $('view').innerHTML = ''; return; }
  const q = qmeta();
  const st = STATUS[q.status] || [q.status, ''];
  const conn = q.connection && Object.keys(q.connection).length
    ? Object.entries(q.connection).filter(([,v]) => v).map(([k,v]) => `${esc(k)}=<b style="color:var(--text)">${esc(v)}</b>`).join('  ') : '—';
  const flagTotal = q.flag_count || 1, flagGot = q.correct_flag_count || 0;
  const head = `<div class="card"><div class="pad">
    <div class="qhead">
      <h1>${esc(q.title || q.qid)}</h1>
      <span class="pill ${st[1]}">${st[0]}</span>
      ${flagTotal > 1 ? `<span class="pill ${flagGot >= flagTotal ? 'ok' : 'run'}">flag ${flagGot}/${flagTotal}</span>` : ''}
      <span class="pill">${esc(q.category || '-')}</span>
      <span class="pill">${q.score || 0} 分</span>
    </div>
    <div class="kv">
      <div class="row"><span class="k">题目 ID</span><span class="v mono">${esc(q.qid)}</span></div>
      <div class="row"><span class="k">目标</span><span class="v">${conn}</span></div>
      <div class="row"><span class="k">工作区</span><span class="v mono">${esc(q.workdir || '-')}</span></div>
      <div class="row"><span class="k">尝试</span><span class="v">${q.attempts || 0} 次${q.elapsed != null ? ' · 本轮已跑 ' + fmtSec(q.elapsed) : ''}</span></div>
    </div>
    ${flagTotal > 1 ? `<div class="bar"><i style="width:${Math.round(flagGot / flagTotal * 100)}%"></i></div>` : ''}
    ${q.description ? `<div class="desc">${esc(q.description)}</div>` : ''}
  </div></div>`;

  const mode = (qdata && qdata.mode) || 'waiting';
  const graphCard = mode === 'attack' ? attackCard(qdata.graph)
                  : mode === 'placeholder' ? placeholderCard(q) : waitingCard();
  const chainCard = chainHTML(qdata && qdata.chain);
  const findCard = findingsHTML(qdata && qdata.findings);
  const siteC = siteCard(qdata && qdata.site);
  const workersCard = workersHTML(q);
  const subsCard = subsHTML(qdata && qdata.subs);

  /* 关键:走 setHtml 而不是直接 innerHTML —— 内容没变就一个字节都不动,
     用户展开的黑板、滚到一半的位置、点开的节点详情才能扛过每 2 秒的轮询(实测 bug)。 */
  const gsig = (qdata && qdata.graph)
    ? hash(qdata.graph.nodes.map(n => n.id + n.label + n.severity + n.kind).join('|') + '#' + qdata.graph.edges.length)
    : 0;
  const wrote = setHtml($('view'), `<div class="wrap">${head}${siteC}${graphCard}${chainCard}${findCard}${workersCard}${subsCard}</div><!--g${gsig}-->`);
  if (wrote){
    drawGraph();
    if (selNode) showNode(selNode, true);
  } else if (!$('atk_svg') && qdata && qdata.graph){ drawGraph(); }
}
/* ---- 涉诈站点档案卡:00_档案 + 报告/证据/导图清单 + 该站常驻 ---- */
function siteCard(site){
  if (!site) return '';
  const md = t => `<div class="desc" style="max-height:380px;overflow:auto">${mdRender(t)}</div>`;
  const files = l => (l && l.length) ? l.map(esc).join('、') : '—';
  const rows = (site.residents || []).map(r =>
    `<tr><td>${esc(r.name)}</td><td>${esc(r.status)}${r.status.startsWith('运行中') && !r.pid_alive ? ' <span style="color:var(--text-faint)">(PID 死,僵尸行)</span>' : ''}</td><td class="t">${esc(r.use)}</td><td class="t">${esc(r.started)}</td></tr>`).join('');
  return `<div class="card"><h2>站点档案 <span class="sub mono">${esc(site.archive || '')}</span></h2><div class="pad">
    ${site.profile_md ? md(site.profile_md) : '<div class="empty">还没建 00_档案.md(worker 首轮交付时会落盘)</div>'}
    <div class="kv" style="margin-top:10px">
      <div class="row"><span class="k">报告</span><span class="v">${files(site.reports)}</span></div>
      <div class="row"><span class="k">思维导图</span><span class="v">${files(site.mindmaps)}</span></div>
      <div class="row"><span class="k">证据(最近)</span><span class="v">${files((site.evidence || []).slice(-8))}</span></div>
    </div>
    ${rows ? `<table style="margin-top:10px"><thead><tr><th>常驻脚本</th><th>状态</th><th>用途</th><th>启动</th></tr></thead><tbody>${rows}</tbody></table>`
           : '<div class="hint" style="margin-top:8px">该站暂无常驻脚本。</div>'}
  </div></div>`;
}
function placeholderCard(q){  return `<div class="card"><h2>攻击链路图 <span class="sub">${esc(q.category || '')} 题</span></h2><div class="pad">
    <div class="empty">这道题不是渗透题(没有网络目标),暂时不画攻击链路图。<br>
      链路图表达的是"入口 → 服务 → 利用 → 拿到什么",pwn / 逆向 / 密码学的解题过程是"写脚本 → 出结果",画成链路不是它的形状 —— 这类题的展示方式待定,这里先把位置留出来。</div>
  </div></div>`;
}
function waitingCard(){
  return `<div class="card"><h2>攻击链路图</h2><div class="pad"><div class="empty">
    还没有探测到网络目标。agent 摸到端口/路径、写下脚本、或黑板出现"已确认"条目后,链路图会自动长出来。</div></div></div>`;
}
/* ---- 攻击链路图:列式分层,结构上不会重叠 ---- */
function attackCard(g){
  if (!g || !g.nodes || !g.nodes.length){
    return `<div class="card"><h2>攻击链路图</h2><div class="pad"><div class="empty">还没有可画的节点 —— agent 探测到端口/路径、写下脚本、或黑板上出现"已确认"条目后,这里会自动长出来。</div></div></div>`;
  }
  const s = g.target.stats;
  const stat = (k, v) => `<span style="color:${v ? 'var(--text-meta)' : 'var(--text-faint)'}">${k} <b style="color:${v ? 'var(--text)' : 'var(--text-faint)'}">${v}</b></span>`;
  return `<div class="card"><h2>攻击链路图
      <span class="sub">${g.nodes.length} 节点 · ${g.edges.length} 边 · ${g.slots.length} 个 agent</span>
      <span class="right">${stat('端点', s.endpoints)} ${stat('工具', s.tools)} ${stat('脚本', s.scripts)} ${stat('发现', s.findings)}</span>
    </h2><div class="pad"><div class="atk">
      <div>
        <div class="canvas"><svg id="atk_svg" viewBox="0 0 ${W} 300" width="100%" style="height:auto"></svg></div>
        <div class="legend">
          ${Object.entries(NCOLOR).map(([k, c]) => `<span><i style="background:${c}"></i>${({target:'目标',endpoint:'探测端点',tool:'工具',script:'脚本',finding:'发现',goal:'FLAG'})[k]}</span>`).join('')}
          <span><i style="background:var(--success)"></i>拿到 flag</span>
          <span><i style="background:var(--text-faint)"></i>其它动作</span>
        </div>
      </div>
      <div>
        <div class="card" style="margin:0"><h2>发现的服务 / 工具 / 脚本 <span class="right">${g.services.length} 条</span></h2>
          <div class="pad" style="max-height:300px;overflow:auto" class="scroll">
            ${g.services.length ? g.services.map((n, i) =>
              `<div class="nd" data-nid="${n.id}" onclick="showNode('${n.id}')">
                 <span class="c" style="background:${NCOLOR[n.kind]}"></span>
                 <span class="m"><span class="n">${esc(n.label)}</span>
                 <span class="s">${({endpoint:'探测端点',tool:'工具',script:'脚本'})[n.kind] || n.kind}${(n.slots && n.slots.length ? ' · ' + n.slots.join('/') : n.slot ? ' · ' + esc(n.slot) : '')}${n.sub ? ' · ' + esc(n.sub) : ''}</span></span></div>`).join('')
              : '<div class="empty">还没记录到工具/端点/脚本</div>'}
          </div></div>
        <div id="atk_detail"></div>
      </div>
    </div></div></div>`;
}
function drawGraph(){
  const g = qdata && qdata.graph, svg = $('atk_svg');
  if (!g || !svg) return;
  /* 列式分层:每种节点占一列,同一列按行等距排 —— 结构上不可能重叠(旧版按"同层横向均分"排,
     标签全叠在一起还被截成乱码)。一列超过 ROWS 行就折成多个子栏,避免拉成一条长龙。 */
  // ROW_H 要留出“框(22px) + 名称 + 用途副标题”三层的高度:44 会让副标题顶到下一个框的顶边
  const ROWS = 11, SUB_W = 112, GAP = 58, PAD_T = 46, ROW_H = 58, PAD_X = 34;
  const groups = {};
  g.nodes.forEach(n => {
    const k = (n.kind === 'tool' || n.kind === 'script') ? 'ts' : n.kind;
    (groups[k] = groups[k] || []).push(n);
  });
  const pos = {}; let cursor = PAD_X, maxRows = 1; const single = []; const goalStack = [];
  ['target','endpoint','ts','finding','goal'].forEach(kind => {
    const arr = groups[kind];
    if (!arr || !arr.length) return;
    groups[kind] = null;
    if (kind === 'target'){
      const n = arr[0];
      pos[n.id] = {x: cursor + 74, y: 0};      // y 稍后按整图高度垂直居中
      single.push(n.id);
      cursor += 148 + GAP;
      return;
    }
    if (kind === 'goal'){
      // 一题多 flag(实测 b-02 有 6 个)要每个 flag 一个 FLAG 节点:早期只给 arr 的最后一个算坐标,
      // 其余节点没有 pos 就画不出来 —— 图上永远只有一个 FLAG。
      arr.forEach((n, i) => { pos[n.id] = {x: cursor + 74, y: PAD_T + i * ROW_H}; });
      goalStack.push(...arr.map(n => n.id));
      maxRows = Math.max(maxRows, Math.min(arr.length, ROWS));
      cursor += 148 + GAP;
      return;
    }
    const sub = Math.ceil(arr.length / ROWS);
    arr.forEach((n, i) => {
      const c = Math.floor(i / ROWS), r = i % ROWS;
      pos[n.id] = {x: cursor + c * SUB_W + SUB_W / 2, y: PAD_T + r * ROW_H};
    });
    maxRows = Math.max(maxRows, Math.min(arr.length, ROWS));
    cursor += sub * SUB_W + GAP;
  });
  const rows = Math.min(maxRows, ROWS);
  const midY = PAD_T + (rows - 1) * ROW_H / 2;
  single.forEach(id => { pos[id].y = midY; });
  // FLAG 节点按整栈垂直居中(多 flag 时是一列)
  goalStack.forEach((id, i) => {
    pos[id].y = midY - (goalStack.length - 1) * ROW_H / 2 + i * ROW_H;
  });
  const W_ = Math.max(560, cursor - GAP + PAD_X);
  const H = Math.max(260, PAD_T + rows * ROW_H + 30);
  svg.setAttribute('viewBox', `0 0 ${W_} ${H}`);
  const byId = {}; g.nodes.forEach(n => byId[n.id] = n);
  const R = n => (n.kind === 'target' ? 0 : n.kind === 'goal' ? 22 : (SEV_R[n.severity] || 9));
  let out = '';
  g.edges.forEach(e => {
    const a = pos[e.from], b = pos[e.to];
    if (!a || !b) return;
    const x1 = a.x + R(byId[e.from]), x2 = b.x - R(byId[e.to]);
    const mx = (x1 + x2) / 2;
    const col = e.kind === 'win' ? 'var(--success)' : e.kind === 'find' ? 'var(--error)' : 'var(--border-hover)';
    const dash = (e.kind === 'win' || e.kind === 'find') ? '' : ' stroke-dasharray="3 3"';
    out += `<path d="M${x1} ${a.y} C${mx} ${a.y} ${mx} ${b.y} ${x2} ${b.y}" fill="none" stroke="${col}"
      stroke-width="${e.kind === 'win' ? 2 : 1.2}"${dash} opacity=".8"/>`;
  });
  g.nodes.forEach(n => {
    const p = pos[n.id]; if (!p) return;
    const r = SEV_R[n.severity] || 9, color = NCOLOR[n.kind] || '#888';
    const label = n.label.length > 17 ? n.label.slice(0, 16) + '…' : n.label;
    let shape;
    if (n.kind === 'target') shape = `<rect x="${p.x-74}" y="${p.y-16}" width="148" height="32" rx="8" fill="${color}" opacity=".32" stroke="${color}" stroke-width="1.6"/>`;
    else if (n.kind === 'goal') shape = `<polygon points="${p.x},${p.y-20} ${p.x+20},${p.y-10} ${p.x+20},${p.y+10} ${p.x},${p.y+20} ${p.x-20},${p.y+10} ${p.x-20},${p.y-10}" fill="${color}" opacity=".9"/>`;
    else if (n.kind === 'tool' || n.kind === 'script') shape = `<rect x="${p.x-48}" y="${p.y-11}" width="96" height="22" rx="6" fill="${color}" opacity=".38" stroke="${color}" stroke-width="1.1"/>`;
    else shape = `<circle cx="${p.x}" cy="${p.y}" r="${r}" fill="${color}" opacity=".85"/>`;
    const ly = n.kind === 'target' ? p.y + 27 : p.y + (n.kind === 'goal' ? 34 : r + 14);
    // 副标题:工具/脚本框下面挂"这一步要干什么"(agent 自己写的意图),端点框下面挂端口
    const subRaw = (n.sub || '').trim();
    const sub = subRaw.length > 18 ? subRaw.slice(0, 17) + '…' : subRaw;
    out += `<g class="node" data-nid="${n.id}" onclick="showNode('${n.id}')">
      <title>${esc((n.slots && n.slots.length ? n.slots.join('/') : n.slot ? n.slot : '') + (n.slots && n.slots.length ? ' · ' : '') + n.severity + (subRaw ? ' — ' + subRaw : '') + (n.detail ? '\\n' + n.detail : ''))}</title>
      ${shape}<text x="${p.x}" y="${ly}" text-anchor="middle">${esc(label)}</text>
      ${sub ? `<text class="sub" x="${p.x}" y="${ly + 11}" text-anchor="middle">${esc(sub)}</text>` : ''}</g>`;
  });
  svg.innerHTML = out;
}
function bindGraph(){ drawGraph(); }
function showNode(id){
  const g = qdata && qdata.graph; if (!g) return;
  selNode = id;
  const n = g.nodes.find(x => x.id === id); if (!n) return;
  document.querySelectorAll('.node.sel,.nd.sel').forEach(e => e.classList.remove('sel'));
  document.querySelectorAll(`[data-nid="${id}"]`).forEach(e => e.classList.add('sel'));
  const ins = g.edges.filter(e => e.to === id).map(e => `${byIdLabel(g, e.from)} --${e.kind}-->`).join('<br>');
  const outs = g.edges.filter(e => e.from === id).map(e => `--${e.kind}--> ${byIdLabel(g, e.to)}`).join('<br>');
  $('atk_detail').innerHTML = `<div class="detailbox">
    <div class="h"><span class="c" style="width:9px;height:9px;border-radius:99px;background:${NCOLOR[n.kind]}"></span>
      <b style="color:var(--text)">${esc(n.label)}</b>
      <span class="sev ${n.severity}">${n.severity}</span>
      <span style="color:var(--text-faint);font-size:11px">${esc(n.kind)}${n.slot ? ' · ' + esc(n.slot) : ''}${n.time ? ' · ' + esc(n.time) : ''}</span>
      <span class="grow" style="flex:1"></span>
      <button onclick="selNode=null;document.getElementById('atk_detail').innerHTML=''">收起</button></div>
    ${n.detail ? `<pre>${esc(n.detail)}</pre>` : '<div class="empty">没有更多细节</div>'}
    ${ins || outs ? `<div style="margin-top:8px;color:var(--text-faint);font-size:11px">${ins}${ins && outs ? '<br>' : ''}${outs}</div>` : ''}
  </div>`;
}
function byIdLabel(g, id){ const n = g.nodes.find(x => x.id === id); return n ? esc(n.label) : id; }

/* ---- 解题链 ---- */
function chainHTML(chain){
  if (!chain || !chain.length){
    return `<div class="card"><h2>解题链</h2><div class="pad"><div class="empty">还没有黑板记录 —— agent 会在动手前先写一行计划。</div></div></div>`;
  }
  const lanes = {};
  chain.forEach(e => { const k = e.slot || 'sys'; (lanes[k] = lanes[k] || []).push(e); });
  const body = Object.entries(lanes).map(([slot, evs]) => `<div class="lane">
      <div class="who">${esc(slot)}</div>
      <div>${evs.map(e => `<div class="ev ${e.kind}">
        <span class="tm">${esc(e.time || '')}</span><span class="tx">${esc(e.text)}</span></div>`).join('')}</div>
    </div>`).join('');
  return `<div class="card"><h2>解题链 <span class="sub">按 agent 分泳道 · 共 ${chain.length} 条</span></h2><div class="pad"><div class="lanes">${body}</div></div></div>`;
}
/* ---- 收获 ---- */
function findingsHTML(f){
  if (!f) return '';
  const fl = f.findings || [], fails = f.fails || [], arts = f.artifacts || [];
  return `<div class="card"><h2>收获
      <span class="sub">${fl.length} 条已确认发现${fails.length ? ' · ' + fails.length + ' 次失败' : ''}${arts.length ? ' · ' + arts.length + ' 个产物' : ''}</span></h2>
    <div class="pad">
      ${fl.length ? fl.map(x => `<div class="fc"><div class="h">
          <span class="sev ${x.severity}">${x.severity}</span>
          <span>${esc(x.slot || '')}${x.time ? ' · ' + esc(x.time) : ''}</span></div>
        <div class="b">${esc(x.text)}</div></div>`).join('') : '<div class="empty">还没有"已确认"级别的发现</div>'}
      ${arts.length ? `<div style="margin-top:10px"><div class="hint" style="margin:0 0 6px">agent 写下的脚本/笔记</div>
        <div class="arts">${arts.map(a => `<span class="a" title="${esc(a.path)}">${esc(a.slot)}/${esc(a.name)} · ${Math.round(a.size/1024)}K</span>`).join('')}</div></div>` : ''}
      ${fails.length ? `<details><summary>失败的尝试(${fails.length})</summary><pre>${fails.map(x => esc((x.time || '') + ' [' + (x.slot || '') + '] ' + x.text)).join('\\n')}</pre></details>` : ''}
    </div></div>`;
}
/* ---- agent 轨迹 + 黑板 ---- */
function workersHTML(q){
  const ws = (q.workers || []);
  const shared = (qdata && qdata.shared) || '';
  return `<div class="card"><h2>Agent 实时轨迹 <span class="sub">${ws.length} 个在跑</span></h2><div class="pad">
    ${ws.length ? ws.map(w => `<div class="wk">
      <div class="wh"><span class="pill run">w${w.slot}</span><span>运行 ${fmtSec(w.age)}</span>
        <span>静默 ${fmtSec(w.quiet)}</span><span>tokens ${w.tokens || '-'}</span>
        ${w.quiet > 180 ? '<span class="pill warn">可能卡住</span>' : ''}</div>
      ${(w.actions || []).map(a => `<div class="act">${esc(a)}</div>`).join('')}
      ${w.speech ? `<div class="act say">💬 ${esc(w.speech)}</div>` : ''}
      ${(w.errors || []).map(e => `<div class="act err">⚠ ${esc(e)}</div>`).join('')}
    </div>`).join('') : '<div class="empty">当前没有 agent 在跑这道题</div>'}
    ${shared ? `<details><summary>共享黑板 SHARED.md(agent 之间的沟通记录)</summary><pre>${esc(shared)}</pre></details>`
             : '<div class="empty" style="margin-top:8px">还没有共享黑板</div>'}
  </div></div>`;
}
/* ---- 提交记录 ---- */
function subsHTML(subs){
  if (!subs || !subs.length) return '';
  return `<div class="card"><h2>本题提交记录 <span class="right">${subs.length} 次</span></h2><div class="pad">
    <table><thead><tr><th style="width:74px">时间</th><th style="width:52px">结果</th><th>flag</th></tr></thead>
    <tbody>${subs.map(s => `<tr><td class="mono">${esc((s.ts || '').slice(11))}</td>
      <td>${s.ok ? '<span class="pill ok">正确</span>' : '<span class="pill bad">错误</span>'}</td>
      <td class="mono">${esc(s.flag)}</td></tr>`).join('')}</tbody></table>
  </div></div>`;
}

/* ---------------- 全局记录 ---------------- */
function renderRecords(){
  if (!state) return;
  const subs = state.submissions || [];
  setHtml($('subs'), subs.length ? `<table><thead><tr><th style="width:74px">时间</th><th style="width:90px">题目</th>
      <th style="width:52px">结果</th><th>flag</th></tr></thead><tbody>
    ${subs.map(s => `<tr><td class="mono">${esc((s.ts || '').slice(11))}</td><td class="mono">${esc(s.question_id)}</td>
      <td>${s.ok ? '<span class="pill ok">正确</span>' : (s.accepted ? '<span class="pill bad">错误</span>' : '<span class="pill warn">未受理</span>')}</td>
      <td class="mono">${esc(s.flag)}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">还没有提交</div>');
  $('subs_n').textContent = subs.length ? '最近 ' + subs.length + ' 次' : '';
  const facts = state.facts || [];
  setHtml($('facts'), facts.length ? facts.map(f => `<div class="ev obs" style="border-left-color:var(--border)">
      <span class="tm">${esc(f.ts)}</span><span class="tx"><b style="color:var(--text-meta)">${esc(f.source)}</b> ${esc(f.text)}</span></div>`).join('')
    : '<div class="empty">还没有事件</div>');
}

/* ---------------- 配置 ---------------- */
function fillConfig(){
  if (!state) return;
  const cfg = state.config || {};
  FIELDS.forEach(k => { const el = $('f_' + k); if (el) el.value = cfg[k] ?? ''; });
}
async function saveConfig(){
  const o = {}; FIELDS.forEach(k => { const el = $('f_' + k); if (el) o[k] = el.value; });
  const r = await post('/api/config', o);
  $('msg_cfg').textContent = r.message;
  toast(r.message, !r.ok);
}
async function act(a){
  if (a === 'test'){ $('msg_model').textContent = '测试中…';
    const r = await post('/api/test-model'); $('msg_model').textContent = r.message; return; }
  if (a === 'test-contest'){ $('msg_platform').textContent = '测试中…';
    const r = await post('/api/test-contest'); $('msg_platform').textContent = r.message; return; }
  if (a === 'test-vpn'){ $('msg_platform').textContent = 'VPN 预检中…';
    const r = await post('/api/test-vpn'); $('msg_platform').textContent = r.message; return; }
  const r = await post('/api/runner', {action: a, project: curProject});
  toast(r.message, !r.ok); tick();
}
/* ---------------- 知识库 ---------------- */
async function loadKbList(){
  const d = await get('/api/knowledge');
  setHtml($('kb_list'), (d.items || []).map((it, i) => `<div class="k" onclick="showKb('${esc(it.path)}')">${esc(it.name)}</div>`).join('')
    || '<div class="empty">知识库为空</div>');
}
async function showKb(rel){
  const d = await get('/api/knowledge/' + encodeURIComponent(rel));
  const v = $('kb_view'); v.style.display = 'block'; v.textContent = d.content || '(空)';
  $('kb_results').innerHTML = '';
}
async function kbSearch(){
  const q = ($('kb_q').value || '').trim(); if (!q) return;
  const d = await get('/api/kb-search?q=' + encodeURIComponent(q));
  $('kb_view').style.display = 'none';
  $('kb_results').innerHTML = `<div class="card" style="margin:0"><h2>检索「${esc(q)}」</h2><div class="pad"><pre>${esc(d.results || '(无结果)')}</pre></div></div>`;
}

/* ---------------- 涉诈自动批次 ---------------- */
let szState = null;
function szMsg(id, text){ const el = $(String(id).replace(/^#/, '')); if (el) el.textContent = text; }
async function szPost(o, msgId){
  try {
    const r = await post('/api/shezha', o);
    if (r.state){ szState = r.state; renderSz(); }
    szMsg(msgId || '#sz_msg2', (r.ok ? '' : '✘ ') + (r.message || ''));
    if (r.output) $('sz_close_out').textContent = r.output;
    return r;
  } catch(e){ szMsg(msgId || '#sz_msg2', '请求失败: ' + e); }
}
/* 涉诈批次卡片的渲染目标全在「设置」抽屉里。抽屉关着的时候每 2.5s 重写一遍
   (批次表 + 常驻表 + 同步状态)是纯浪费的主线程时间 —— 只更新数据,DOM 等抽屉真开了再画。 */
let szDrawerPainted = false;
function szDrawerOpen(){ const el = $('d_settings'); return !!(el && el.classList.contains('open')); }
async function szTick(){
  if (document.hidden) return;
  try { const d = await get('/api/shezha'); szState = d; if (szDrawerOpen()) renderSz(); } catch(e){}
}
function renderSz(){
  const d = szState; if (!d) return;
  $('sz_setup').hidden = !!d.configured;
  $('sz_main').hidden = !d.configured;
  if (!d.configured) return;
  $('sz_root_cur').textContent = d.root + (d.ready ? '' : '  ⚠ 目录里没找到 excel转targets.py / 开批次.sh');
  const run = d.runner || {}, prog = d.progress || {}, close = d.close;
  let s = '批次 ' + (d.batch ?? '?') + ' · 本批目标 ' + (d.targets || []).length + ' 个 · 交付 '
        + (prog.solved || 0) + '/' + (prog.total || 0) + ' · findings ' + (prog.findings || 0)
        + ' · runner ' + (run.running ? '运行中(pid ' + run.pid + ')' : '未运行');
  if (d.lane) s += ' · 车道: ' + d.lane;
  const rz = d.residents || {};
  if (rz.running != null) s += ' · 常驻 ' + rz.running + ' 路' + (rz.zombie ? '(⚠ ' + rz.zombie + ' 路 PID 已死,该清理/登记)' : '');
  if (close) s += ' · 收工: ' + (close.running ? '进行中…' : (close.rc === 0 ? '完成' : '失败 rc=' + close.rc))
                + (close.finished ? ' @ ' + close.finished : '');
  $('sz_summary').textContent = s;
  if (close && !close.running && close.output) $('sz_close_out').textContent = close.output;
  const rows = (d.targets || []).map(t =>
    '<tr><td>' + esc(t.id || '') + '</td><td class="t">' + esc(t.src || '') + '</td></tr>').join('');
  const gRows = ((rz.groups || []).slice(0, 8)).map(g =>
    '<tr><td>' + esc(g.site) + '</td><td>' + g.running + '/' + g.total + ' 运行中</td></tr>').join('');
  $('sz_targets').innerHTML = rows
    ? '<table><thead><tr><th>本批目标</th><th>来源</th></tr></thead><tbody>' + rows + '</tbody></table>'
      + (gRows ? '<table style="margin-top:8px"><thead><tr><th>有常驻的站点(全部见各站详情)</th><th>常驻</th></tr></thead><tbody>' + gRows + '</tbody></table>' : '')
    : '<div class="empty">还没有本批目标 —— 上传 Excel 切批次</div>';
  renderSzSync(d);
}
function renderSzSync(d){
  const box = $('sz_sync'); if (!box) return;
  const s = d.autosync || {}, p = s.pending || {};
  const on = d.autosync_on !== false;
  const parts = [];
  parts.push(on ? '<span class="pill ok">自动同步 开</span>' : '<span class="pill warn">自动同步 关</span>');
  parts.push('每 ' + (s.tick || 180) + 's 扫仓库');
  if (p.sites) parts.push(p.sites + ' 站/' + (p.domains || p.sites) + ' 域名');
  if (p.todo_review) parts.push('<span class="pill warn">待评 ' + p.todo_review + '</span>');
  if (p.unclustered) parts.push('<span class="pill">待归簇 ' + p.unclustered + '</span>');
  if (p.resign) parts.push('<span class="pill">补指纹 ' + p.resign + '</span>');
  if (s.last_change) parts.push('上次同步 ' + s.last_change);
  else if (s.last_tick) parts.push('上次扫描 ' + s.last_tick);
  if (s.actions && s.actions.length) parts.push('↦ ' + s.actions.join(' / '));
  box.innerHTML = parts.join(' · ');
}
async function szConfig(){
  const root = $('sz_root').value.trim();
  if (!root){ szMsg('#sz_msg', '先填作战仓库根目录'); return; }
  await szPost({action: 'config', root: root,
                pi_provider: $('sz_pi_provider').value.trim(), pi_model: $('sz_pi_model').value.trim()}, '#sz_msg');
}
async function szConfig2(){
  await szPost({action: 'config', root: $('sz_root2').value.trim() || (szState && szState.root),
                pi_provider: $('sz_pi_provider2').value.trim(), pi_model: $('sz_pi_model2').value.trim()}, '#sz_msg3');
}
function szReconfig(){
  const d = szState || {};
  $('sz_root2').value = d.root || '';
  $('sz_pi_provider2').value = d.pi_provider || '';
  $('sz_pi_model2').value = d.pi_model || '';
  $('sz_setup2').hidden = !$('sz_setup2').hidden;
}
async function szUpload(){
  const fs = $('sz_files').files;
  if (!fs.length){ szMsg('#sz_msg2', '先选 Excel 文件'); return; }
  szMsg('#sz_msg2', '上传转换中…(' + fs.length + ' 个文件)');
  const files = [];
  for (const f of fs){
    const b64 = await new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res(String(r.result).split(',')[1]);
      r.onerror = () => rej(r.error);
      r.readAsDataURL(f);
    });
    files.push({name: f.name, data_b64: b64});
  }
  await szPost({action: 'excel', files: files,
                batch_size: +$('sz_bs').value || 10,
                batch_no: $('sz_bn').value ? +$('sz_bn').value : null,
                lib: $('sz_lib').value.trim() || null,
                redo: $('sz_redo').value.trim() || null});
  $('sz_files').value = '';
}
async function szRunner(a){ await szPost({action: 'runner', runner_action: a}); }
async function szClose(scribe){
  const b = (szState && szState.batch) || 0;
  if (!b){ szMsg('#sz_msg2', '拿不到批次号 —— 先切批次'); return; }
  await szPost({action: 'close', batch: b, scribe: scribe});
}
szTick(); setInterval(szTick, 2500);

/* ---------------- Provider 档案库 ---------------- */
let PROV = {saved: [], pi: []};
async function loadProv(){
  try { PROV = await get('/api/providers'); renderProv(); } catch(e){}
}
function renderProv(){
  const sel = document.getElementById('f_saved_prov');
  if (!sel) return;
  sel.innerHTML = (PROV.saved || []).map(p =>
    `<option value="${esc(p.name)}">${esc(p.name)} · ${esc(p.model || p.base_url || '?')}${p.key_mask && p.key_mask !== '(空)' ? ' · ' + esc(p.key_mask) : ''}</option>`).join('')
    || '<option value="">(还没有存过 Provider)</option>';
  // shezha 卡片的 provider/model 提示列表
  const pd = document.getElementById('pi_prov_list'), md = document.getElementById('pi_model_list');
  if (pd) pd.innerHTML = (PROV.pi || []).map(x => `<option value="${esc(x.name)}">`).join('');
  if (md){
    const cur = (document.getElementById('sz_pi_provider2')||{}).value || '';
    const hit = (PROV.pi || []).find(x => x.name === cur);
    md.innerHTML = (hit && hit.models ? hit.models : []).map(m => `<option value="${esc(m)}">`).join('');
  }
}
function provFormVals(){
  return {PI_BASE_URL: $('f_PI_BASE_URL').value.trim(), PI_API_KEY: $('f_PI_API_KEY').value.trim(),
          PI_MODEL: $('f_PI_MODEL').value.trim(), PI_API_TYPE: $('f_PI_API_TYPE').value};
}
function provFillForm(){
  const name = document.getElementById('f_saved_prov').value;
  const e = (PROV.saved || []).find(x => x.name === name);
  if (!e) return;
  $('f_PI_BASE_URL').value = e.base_url || ''; $('f_PI_API_KEY').value = '';
  $('f_PI_MODEL').value = e.model || ''; $('f_PI_API_TYPE').value = e.api_type || 'openai-completions';
  $('msg_prov').textContent = '已填入「' + name + '」(Key 不回显,应用时从档案取;要改 Key 就重存)';
  dirty = true;
}
async function provApply(){
  const name = document.getElementById('f_saved_prov').value;
  if (!name){ $('msg_prov').textContent = '先选一个 Provider'; return; }
  const r = await post('/api/providers', {prov_action: 'apply', name});
  $('msg_prov').textContent = r.message;
  if (r.ok){ dirty = false; fillConfig(); toast(r.message); }
}
async function provSave(){
  const name = prompt('给这组模型配置起个名字(如 glm-shezha / ds备用):');
  if (!name) return;
  const r = await post('/api/providers', Object.assign({prov_action: 'save', name: name.trim()}, provFormVals()));
  $('msg_prov').textContent = r.message;
  if (r.ok){ PROV = r.providers; renderProv();
    const sel = document.getElementById('f_saved_prov');
    if ([...sel.options].some(o => o.value === name.trim())) sel.value = name.trim();
  }
}
async function provDelete(){
  const name = document.getElementById('f_saved_prov').value;
  if (!name || !confirm('删除 Provider「' + name + '」?')) return;
  const r = await post('/api/providers', {prov_action: 'delete', name});
  $('msg_prov').textContent = r.message;
  if (r.ok){ PROV = r.providers; renderProv(); }
}
loadProv();

/* ---------------- 涉诈:簇树 / 单站视图 / 时间线 / 周报 / 总Agent巡检 ---------------- */
let szTree = null, szTreeAt = 0, szCur = null, szTlLoaded = false, _revShownDone = false, _szClName = null;
let szViewMode = 'clusters', szOnlyHits = false, szOnlyReview = false, szOnlyArchived = false, szGraphMode = 'graph';
let viewRestored = false, scrollTimer = null, tickSeq = 0;
function readViewState(){
  const raw = location.hash.replace(/^#(?:rotom)?\\?/, '');
  if (!raw || raw === location.hash) return {};
  try { return Object.fromEntries(new URLSearchParams(raw).entries()); } catch(e){ return {}; }
}
/* 键要能在"页面刚加载、运行时变量还没填上"时也算对,否则刷新后读的是另一个键(实测刷新后
   _szClName 还是空,键末尾两个空,存的值永远取不到)。所以优先用 URL hash 里的簇/站点/题目,
   运行时变量只作兜底。 */
function viewScrollKey(){
  const v = readViewState();
  const cluster = v.cluster || _szClName || '';
  const site = v.site || szCur || '';
  const qid = v.qid || sel || '';
  return 'rotom-scroll:' + [v.project || curProject || '', cluster, site, qid].join('|');
}
/* 滚动位置:实测每个 scroll 事件同步写一次 sessionStorage 要 13ms,滚动时每帧都在主线程
   上做序列化+写存储,这就是"卡"的直接来源。改成 200ms 尾沿节流 + 值没变不写。
   恢复则推迟到内容真的渲染出来之后(见 scheduleRestore),否则容器高度不足会被浏览器夹成 0。 */
let _scrollDirty = false, _scrollTimer = 0, _lastSaved = '', _lastScrollKey = '', _restoreTimer = 0;
function scrollEls(){
  return {main: document.querySelector('main'), tasks: $('tasks'),
          graph: document.querySelector('.sz-graph-view'), rel: document.querySelector('.sz-rel-view')};
}
function writeScrollState(){
  const e = scrollEls(), key = viewScrollKey();
  const vals = {main: e.main ? e.main.scrollTop : 0, tasks: e.tasks ? e.tasks.scrollTop : 0,
                graph: e.graph ? e.graph.scrollTop : 0, rel: e.rel ? e.rel.scrollTop : 0};
  const cur = key + '|' + [vals.main, vals.tasks, vals.graph, vals.rel].join(',');
  if (cur === _lastSaved) return;
  _lastSaved = cur;
  try { sessionStorage.setItem(key, JSON.stringify(vals)); } catch(err){}
}
function saveScrollState(){
  _scrollDirty = true;
  clearTimeout(_scrollTimer);
  _scrollTimer = setTimeout(() => { if (_scrollDirty){ _scrollDirty = false; writeScrollState(); } }, 200);
}
/* 内容渲染完再落位:轮询几轮直到容器高度够放得下目标位置,或确认用户已经自己滚过了。 */
let _restoreTarget = null, _restoreFrom = null;
function scheduleRestore(){
  /* 已经有待恢复的目标就别覆盖:每轮轮询都会调这里,反复重读会把进行中的恢复打回原点。
     只有"目标已完成"或"刚开始"时才重新取。 */
  if (_restoreTarget) return;
  try {
    const raw = sessionStorage.getItem(viewScrollKey());
    if (!raw) return;
    _restoreTarget = JSON.parse(raw); _restoreFrom = performance.now();
  } catch(e){ _restoreTarget = null; }
}
let _userScrolled = false, _progUntil = 0;
function nudgeRestore(){
  if (!_restoreTarget) return;
  if (_userScrolled){ _restoreTarget = null; return; }   // 用户自己滚了,以用户为准
  const e = scrollEls();
  let ok = true;
  _progUntil = performance.now() + 150;   // 下面 setTop 触发的 scroll 事件是我们自己,别当成用户滚动
  for (const k of ['main','tasks','graph','rel']){
    const el = e[k], want = _restoreTarget[k] || 0;
    if (!el || !want) continue;
    const max = el.scrollHeight - el.clientHeight;
    if (max <= 0){ ok = false; continue; }
    if (max < want - 4){ ok = false; continue; }
    el.scrollTop = want;
  }
  if (ok || performance.now() - _restoreFrom > 12000) _restoreTarget = null;
}
/* 进入新视图(开站/开簇/换项目/刷新)时调用:作废旧目标、重置"用户已滚"标记,
   再按当前视图的键载入待恢复位置 —— 恢复只在该视图第一次渲染后做一次。 */
function beginViewRestore(){
  _restoreTarget = null; _userScrolled = false;
  scheduleRestore();
}
function persistViewState(){
  try {
    const p = new URLSearchParams(); p.set('project', curProject || '');
    if (curProject === '涉诈批次'){
      p.set('view', szViewMode); p.set('hits', szOnlyHits ? '1' : '0');
      p.set('rv', szOnlyReview ? '1' : '0'); p.set('arc', szOnlyArchived ? '1' : '0');
      p.set('graph', szGraphMode);
      if (_szClName) p.set('cluster', _szClName); if (szCur) p.set('site', szCur);
    } else {
      if (sel) p.set('qid', sel); if (filter && filter !== 'all') p.set('filter', filter);
      const kw = (($('q') || {}).value || '').trim(); if (kw) p.set('q', kw);
    }
    const drawer = [...document.querySelectorAll('.drawer.open')].map(d => d.id.replace(/^d_/, '')).join(',');
    if (drawer) p.set('drawer', drawer);
    history.replaceState(null, '', '#rotom?' + p.toString());
    saveScrollState();
  } catch(e){}
}
function applyViewState(){
  const v = readViewState();
  if (v.project) curProject = v.project;
  filter = v.filter || 'all';
  if ($('q')) $('q').value = v.q || '';
  szViewMode = v.view === 'sites' ? 'sites' : 'clusters'; szOnlyHits = v.hits === '1';
  szOnlyReview = v.rv === '1'; szOnlyArchived = v.arc === '1';
  if (szOnlyArchived){ szOnlyHits = false; szOnlyReview = false; }   // 归档视图与"战果/待审"互斥
  szGraphMode = v.graph === 'relations' ? 'relations' : 'graph';
  _szClName = v.cluster || null; szCur = v.site || null;
  sel = curProject === '涉诈批次' ? null : (v.qid || null);
  if (v.drawer) v.drawer.split(',').filter(Boolean).forEach(openDrawer);
}
async function restoreViewLocation(){
  if (viewRestored || !state) return;
  viewRestored = true;
  const v = readViewState();
  if (state.project === '涉诈批次'){
    if (v.site) await szPick(v.site);
    else if (v.cluster) await szPickCluster(v.cluster);
  } else if (sel){ await loadQuestion(true); }
  beginViewRestore();
}
applyViewState();
document.addEventListener('scroll', e => {
  const t = e.target;
  if (!t || !t.nodeType) return;
  if (!(t === document.querySelector('main') || t === $('tasks') || t.classList?.contains('sz-graph-view') || t.classList?.contains('sz-rel-view'))) return;
  if (performance.now() < _progUntil) return;   // nudgeRestore 自己 setTop 触发的事件,不是用户在滚
  _userScrolled = true; _restoreTarget = null;  // 用户动手了:作废恢复,以用户为准(这是"抽一下弹回去"的根治点)
  saveScrollState();
}, true);
window.addEventListener('hashchange', () => { viewRestored = false; applyViewState(); tick(); });
function szSetView(v){ szViewMode = v; renderSzTree(); persistViewState(); }
function szToggleHits(){ szOnlyHits = !szOnlyHits; if (szOnlyHits){ szOnlyReview = false; szOnlyArchived = false; } renderSzTree(); persistViewState(); }
function szToggleReview(){ szOnlyReview = !szOnlyReview; if (szOnlyReview){ szOnlyHits = false; szOnlyArchived = false; } renderSzTree(); persistViewState(); }
function szToggleArchived(){ szOnlyArchived = !szOnlyArchived; if (szOnlyArchived){ szOnlyHits = false; szOnlyReview = false; } renderSzTree(); persistViewState(); }
/* 档位口径(与 review_results.py 一致):deep 扎实 > mid 有获取 > legit 待人工审核 > surface/none。
   未评审时按机械信号 hint 兜底排序;有成果(deep/mid)在前,没成果在后。 */
function szRank(s){
  const d = (s.review || {}).degree;
  if (d) return ({deep: 0, mid: 1, legit: 2, surface: 3, none: 4})[d] ?? 3.5;
  return s.hint === 'result' ? 1.5 : (s.hint === 'legit' ? 2.5 : 3.5);
}
/* 搜索框对涉诈树也生效:按 域名/档案夹名/定性/实名 过滤(Ctrl+K 聚焦,清空恢复全树) */
function szKw(){ return (($('q')||{}).value || '').trim().toLowerCase(); }
function szKwHit(s, kw){
  if (!kw) return true;
  return ((s.domain || '') + ' ' + (s.name || '') + ' ' + (s.profile || '') + ' ' + (s.identity || '')).toLowerCase().includes(kw);
}
function szEmptyHtml(kind){
  const kw = (($('q')||{}).value || '').trim();
  return '<div class="empty">' + (kw
    ? '没有匹配 “' + esc(kw) + '” 的' + kind + ' —— 搜索框里还留着关键词, <a href="#" onclick="clearSearch();return false" style="text-decoration:underline">点我清除</a>'
    : '没有匹配的' + kind) + '</div>';
}
function szIsHit(s){ const d = (s.review || {}).degree; return d ? ['deep', 'mid'].includes(d) : s.hint === 'result'; }
function szIsLegit(s){ const d = (s.review || {}).degree; return d ? d === 'legit' : s.hint === 'legit'; }
function szIsArchived(s){ return !!(s.archived); }
/* 归档 = 已结案:默认不进任何工作视图(战果/待审都不算它),只在「已归档」chip 里露头 */
function szMatch(s){
  if (szOnlyArchived) return szIsArchived(s);
  if (szIsArchived(s)) return false;
  return szOnlyReview ? szIsLegit(s) : (!szOnlyHits || szIsHit(s));
}
/* 树行里的归档/撤销按钮 + 已归档徽标 */
function szArcTag(s){
  const a = s.archived || {};
  return szIsArchived(s)
    ? `<span class="sz-arctag" title="${esc('归档 ' + (a.at || '') + (a.reason ? ' · ' + a.reason : ''))}">已归档</span>`
    : '';
}
function szArcBtn(s){
  return szIsArchived(s)
    ? `<button class="sz-act show" onclick="event.stopPropagation();szUnarchive('${esc(s.domain)}')" title="撤销归档,回到工作视图">撤销</button>`
    : `<button class="sz-act" onclick="event.stopPropagation();szArchive('${esc(s.domain)}')" title="归档结案:不是涉诈 / 已有结论 —— 退出工作视图,派发侧也不再生成新目标">归档</button>`;
}
async function szLoadTree(force){
  if (!force && szTree && Date.now() - szTreeAt < 30000) return;
  try {
    szTree = await get('/api/shezha/tree' + (force ? '?force=1' : ''));
    szTreeAt = Date.now();
    if (state && state.project === '涉诈批次') renderTasks();
  } catch(e){}
}
/* 簇树有 600+ 行,每 2s 无条件重写一次 DOM 会明显卡顿(还会打断选中的滚动位置)。
   内容哈希没变就整块跳过;变了才重写,并把滚动位置按住不动。 */
function setSzTreeHtml(html){
  const cont = $('tasks');
  if (!cont) return;
  const key = String(hash(html));
  if (cont.dataset.h === key) return;
  const sc = cont.scrollTop;
  cont.dataset.h = key;
  cont.innerHTML = html;
  cont.scrollTop = sc;
}
function renderSzTree(){
  const cont = $('tasks');
  const rv = szState && szState.review;
  if (rv && rv.running){ szMsg('#sz_msg4', 'AI 读报告评审中…(已读完的批次已在树上逐步上色)'); }
  else if (rv && !rv.running && rv.rc != null && !_revShownDone){
    _revShownDone = true;
    szMsg('#sz_msg4', '成果评审完成:' + String(rv.output || '').trim().split('\\n').pop());
    szLoadTree(true);
  }
  if (!szTlLoaded){ szTlLoaded = true; szTimeline(7); }
  if (!szTree){ setHtml($('filters'), ''); cont.innerHTML = '<div class="empty">站点总库加载中…</div>'; return; }
  const hitChips = szTree.review_done
    ? `<span class="chip${szOnlyHits ? ' active' : ''}" style="${szOnlyHits ? '' : 'color:var(--success)'}" onclick="szToggleHits()" title="点击后只显示 扎实/有获取 的站点">${szOnlyHits ? '只看有战果' : '已 AI 审成果'}</span>`
    : '<span class="chip" style="opacity:.6" onclick="szReview()" title="点击开始 AI 读报告评审">未审成果(点我开始审)</span>';
  let nLegit = 0, nLegitTodo = 0, nArc = 0;
  szTree.clusters.forEach(c => c.sites.forEach(s => {
    if (szIsArchived(s)){ nArc++; return; }   // 归档 = 已结案:不算待审,也不进簇成果统计
    if (szIsLegit(s)){ nLegit++; const dec = (s.review || {}).decision; if (!dec) nLegitTodo++; }
  }));
  const rvChip = nLegit
    ? `<span class="chip${szOnlyReview ? ' active' : ''}" style="${szOnlyReview ? '' : 'color:var(--error)'}" onclick="szToggleReview()" title="疑似正规资产(备案号/资质类证据,不算战果)——只显示这些,人工复核是否继续打">${szOnlyReview ? '只看待审' : '待人工审核 ' + nLegitTodo}</span>`
    : '';
  /* 归档视图开着但归档空了(比如刚把最后一站撤销)时 chip 必须留着 —— 否则没有出口,
   用户会卡在空树里(实测踩过:chip 一消失就只能刷新页面)。 */
  const arcChip = (nArc || szOnlyArchived)
    ? `<span class="chip${szOnlyArchived ? ' active' : ''}" onclick="szToggleArchived()" title="${nArc ? '已结案的站点(不是涉诈 / 已交付收工):默认不显示在树里,派发侧也不再生成新目标;点开只看归档,可逐个撤销' : '归档里已经没有站了 —— 点一下回到工作视图'}">${szOnlyArchived ? (nArc ? '只看归档 ' + nArc : '只看归档(空)') : '已归档 ' + nArc}</span>`
    : '';
  /* 待审视图里给一键结案:这批站的复核结论就是"不是涉诈,不用再打" */
  const arcAllChip = (szOnlyReview && nLegitTodo)
    ? `<span class="chip" style="color:var(--error)" onclick="szArchivePending()" title="把这 ${nLegitTodo} 个「待人工审核」的站全部归档结案(可逐个撤销)">↑ 一键归档这 ${nLegitTodo} 站</span>`
    : '';
  setHtml($('filters'),
    `<span class="chip${szViewMode === 'clusters' ? ' active' : ''}" onclick="szSetView('clusters')" title="按簇分组">簇 ${szTree.clusters.length}</span>` +
    `<span class="chip${szViewMode === 'sites' ? ' active' : ''}" onclick="szSetView('sites')" title="全部站点平铺,战果多的在前">站点 ${szTree.total}</span>` +
    hitChips + rvChip + arcChip + arcAllChip +
    (szTree.ai_done ? '' : '<span class="chip" style="opacity:.6" onclick="szCluster()" title="点击开始 AI 归簇">未归簇(点我归簇)</span>'));
  const DEG = {
    deep:   {dot: '<span style="color:var(--success)">●</span>', tag: '<span class="pill ok">成果扎实</span>'},
    mid:    {dot: '<span style="color:var(--warn)">●</span>',   tag: '<span class="pill warn">有获取</span>'},
    legit:  {dot: '<span style="color:var(--error)">●</span>',  tag: '<span class="pill bad">待人工审核</span>'},
    surface:{dot: '<span style="color:var(--text-faint)">○</span>', tag: ''},
    none:   {dot: '<span style="color:var(--text-faint)">○</span>', tag: ''}
  };
  let rows = '';
  const kw = szKw();
  if (szViewMode === 'sites'){
    const all = [];
    szTree.clusters.forEach(c => c.sites.forEach(s => all.push(Object.assign({cluster: c.name}, s))));
    all.sort((a, b) => szRank(a) - szRank(b));
    rows = all.filter(szMatch).filter(s => szKwHit(s, kw))
      .map(s => {
        const r = s.review || {};
        const d = DEG[r.degree] || DEG.surface;
        const tip = r && r.got ? ` title="${esc('[' + s.cluster + '] ' + (r.degree === 'legit' ? '疑似正规:' : '成果:') + r.got + (r.gap ? '｜可更深:' + r.gap : ''))}"` : ` title="${esc('[' + s.cluster + ']')}"`;
        return `<div class="ev${szCur === s.domain ? ' active' : ''}"${tip} style="border-left-color:var(--border);cursor:pointer" onclick="szPick('${esc(s.domain)}')"><span class="tx">${d.dot} <b>${esc(s.domain)}</b>${d.tag} <span class="t" style="color:var(--text-faint)">${esc(s.cluster)}</span></span>${szArcTag(s)}${szArcBtn(s)}</div>`;
      }).join('');
    setSzTreeHtml(rows || szEmptyHtml('站点'));
    return;
  }
  const rowsCl = szTree.clusters.map(c => {
    const agg = c.agg || {};
    const aggs = (szTree.review_done && (agg.deep || agg.mid || agg.legit))
      ? ` <span class="t">·</span> <span style="color:var(--success)">扎实${agg.deep || 0}</span> <span style="color:var(--warn)">有获取${agg.mid || 0}</span>${agg.legit ? ` <span style="color:var(--error)">待审${agg.legit}</span>` : ''}` : '';
    const sites = c.sites
      .filter(szMatch).filter(s => szKwHit(s, kw))
      .map(s => {
      const cur = szCur === s.domain ? ' active' : '';
      const r = s.review || {};
      const d = DEG[r.degree] || (s.hint === 'legit' ? {dot: '<span style="color:var(--error)">◆</span>', tag: ''}
                              : s.hint === 'result' ? {dot: '<span style="color:var(--warn)">◆</span>', tag: ''}
                              : {dot: '<span style="color:var(--text-faint)">◇</span>', tag: ''});
      const st = s.status ? ` <span class="t">[${esc(s.status)}]</span>` : '';
      /* 导图标记:填过内容的才给 🗺(统一结构"有探测就有导图",511 座占位骨架不刷版);
         有目录但没导图 → ◦(结构缺件,该站该出图)。 */
      const mmMeta = s.mm || {};
      const mm = mmMeta.real ? ' 🗺'
               : ((!s.mindmaps || !s.mindmaps.length) ? ' <span class="t" title="统一结构要求报告/思维导图源文件.md + 思维导图.html,该站缺件" style="color:var(--warn)">◦</span>' : '');
      const p = s.profile ? ` <span class="t" style="color:var(--text-faint)">${esc(s.profile.slice(0, 42))}</span>` : '';
      const tip = r && r.got ? ` title="${esc((r.degree === 'legit' ? '疑似正规:' : '成果:') + r.got + (r.gap ? '｜可更深:' + r.gap : ''))}"` : '';
      return `<div class="ev${cur}"${tip} style="border-left-color:var(--border);cursor:pointer;padding-left:16px" onclick="szPick('${esc(s.domain)}')"><span class="tx">${d.dot} <b>${esc(s.domain)}</b>${d.tag}${mm}${st}${p}</span>${szArcTag(s)}${szArcBtn(s)}</div>`;
    }).join('');
    const shown = c.sites.filter(szMatch).filter(s => szKwHit(s, kw)).length;
    if ((szOnlyHits || szOnlyReview || szOnlyArchived || kw) && !shown) return '';
    /* 分母跟着视图口径走:工作视图里归档的不算数(它们已结案),归档视图里只数归档的 */
    const pool = c.sites.filter(s => szOnlyArchived ? szIsArchived(s) : !szIsArchived(s)).length;
    const arcNote = (!szOnlyArchived && c.sites.length !== pool) ? `(已归档 ${c.sites.length - pool})` : '';
    return `<div class="ev obs" style="border-left-color:var(--accent);cursor:pointer" onclick="szPickCluster(this.dataset.n)" data-n="${esc(c.name)}" title="点击查看簇关系图"><span class="tx"><b>${esc(c.name)}</b> <span class="t">· ${shown}/${pool} 站${arcNote}</span>${aggs}</span></div>${sites}`;
  }).join('');
  setSzTreeHtml(rowsCl || szEmptyHtml('站点'));
}
async function szPick(domain){
  szCur = domain; sel = null; qdata = null; persistViewState();
  if (state && state.project === '涉诈批次') renderSzTree();
  $('view').innerHTML = '<div class="wrap"><div class="card"><div class="pad"><div class="empty">加载站点档案…</div></div></div></div>';
  try {
    const s = await get('/api/shezha/site?domain=' + encodeURIComponent(domain));
    if (s.error){ szCur = null; persistViewState(); renderSzTree(); $('view').innerHTML = '<div class="wrap"><div class="card"><div class="pad"><div class="empty">' + esc(s.error) + '</div></div></div></div>'; return; }
    renderSzSite(s);
    beginViewRestore(); nudgeRestore();   // 新视图:只按本视图的键恢复一次,用户一滚就以用户为准
  } catch(e){ toast('站点档案加载失败:' + e, true); }
}
function renderSzSite(s){
  const md = t => `<div class="desc" style="max-height:460px;overflow:auto">${mdRender(t)}</div>`;
  const rv = s.review || {};
  const rvMeta = {deep: ['成果扎实', 'ok'], mid: ['有获取', 'warn'],
                  legit: ['待人工审核', 'bad'], surface: ['仅探测', ''], none: ['无材料', '']}[rv.degree];
  /* legit 档 = 拿到的只是备案号/资质这类"证明站点正规"的材料,不算战果 ——
     按铁律「见正规资产立即停手」给出人工落槌入口:停手(进停打名单)/ 核实过继续打。 */
  const undoBtn = `<button onclick="szDecide('${esc(s.domain)}','undo')" title="撤销复核,回到「待人工审核」;停打名单里的记录也一并撤掉">撤销复核</button>`;
  const decide = (rv.degree === 'legit') ? (
    rv.decision === 'stop' ? `<div class="row"><span class="k">人工复核</span><span class="v"><span class="pill bad">已停手</span> <span class="t">${esc(rv.decided || '')} 记入停打名单,切批次/下批不再派发</span> ${undoBtn}</span></div>`
    : rv.decision === 'go' ? `<div class="row"><span class="k">人工复核</span><span class="v"><span class="pill warn">继续打</span> <span class="t">${esc(rv.decided || '')} 已核实:不因备案/资质停手,按线索继续</span> ${undoBtn}</span></div>`
    : s.archived ? `<div class="row"><span class="k">人工复核</span><span class="v"><span class="t">已归档结案,派发侧不再生成新目标;要重新打先在「归档结案」卡里撤销</span></span></div>`
    : `<div class="row"><span class="k">人工复核</span><span class="v"><span class="t">疑似正规资产,先停手还是继续打,由人定:</span>
        <button onclick="szDecide('${esc(s.domain)}','stop')">停手(记入停打名单)</button>
        <button onclick="szDecide('${esc(s.domain)}','go')">核实过 · 继续打</button>
        <span class="t" id="sz_dec_msg" style="font-size:11.5px;color:var(--text-meta)"></span></span></div>`) : '';
  /* 归档结案:人已经有结论了(不是涉诈 / 已交付收工)→ 从工作视图里收走,派发侧也不再生成新目标。
     与上面「停手」的分工:停手是打法落槌(不碰了),归档是结案(收走);材料原地保留,随时撤销。 */
  const arc = s.archived || {};
  const arcMsg = `<span class="t" id="sz_arc_msg" style="font-size:11.5px;color:var(--text-meta)"></span>`;
  const arcCard = `<div class="card"><h2>归档结案 <span class="sub">不是涉诈 / 已有结论 → 退出工作视图,不再派发;档案与评审原地保留</span></h2><div class="pad"><div class="sz-arcrow">
      ${s.archived
        ? `<span class="pill">已归档</span><span class="t">${esc(arc.at || '')}${arc.reason ? ' · ' + esc(arc.reason) : ''}</span>
           <button onclick="szUnarchive('${esc(s.domain)}')">撤销归档</button>${arcMsg}`
        : `<span class="t">归档理由(可选)</span><input class="sz-arc-in" id="sz_arc_reason" placeholder="如:备案主体为正规企业 / 站点已死无深挖价值 / 已交付结案">
           <button onclick="szArchive('${esc(s.domain)}')">归档结案</button>${arcMsg}`}
    </div></div></div>`;
  const rvCard = (s.review) ? `<div class="card"><h2>成果评读 <span class="sub">${rv.by === 'ai' ? 'AI 通读报告' : '机械信号'}</span></h2><div class="pad">
      <div class="kv kv-wide">
        <div class="row"><span class="k">程度</span><span class="v"><span class="pill ${rvMeta ? rvMeta[1] : ''}">${rvMeta ? rvMeta[0] : rv.degree}</span></span></div>
        <div class="row"><span class="k">到底拿到了什么</span><span class="v">${esc(rv.got || '—')}</span></div>
        ${rv.gap ? `<div class="row"><span class="k">还能怎么更深</span><span class="v">${esc(rv.gap)}</span></div>` : ''}
        ${decide}
      </div></div></div>` : '';
  const files = l => (l && l.length) ? l.map(esc).join('、') : '—';
  const res = (s.residents || []).map(r =>
    `<tr><td>${esc(r.name)}</td><td>${esc(r.status)}${r.status.startsWith('运行中') && !r.pid_alive ? ' <span style="color:var(--text-faint)">(PID 死)</span>' : ''}</td><td class="t">${esc(r.use)}</td><td class="t">${esc(r.started)}</td></tr>`).join('');
  const live = s.live ? `<span class="pill ${s.live.delivered ? 'ok' : 'run'}">本批:${s.live.delivered ? '已交付' : (s.live.status || '在批')}</span>` : '';
  /* ---- 新统一结构(2026-10-07 结构整顿):总报告 / 时间线 / 思维导图对 / 目录清点 ---- */
  const mm = s.mm || {};
  const mmLine = mm.branches
    ? `${mm.branches} 分支 · ${mm.nodes} 节点${mm.real ? '' : ' · <span style="color:var(--warn)">占位骨架(探测后直接在原图增删)</span>'}`
    : '没有导图源文件';
  const mmTodo = !mm.real && (s.report_md || '').length > 800;   // 总报告有内容、图还是骨架 → 按规范该补图
  const repCard = s.report_md ? `<div class="card"><h2>本站总报告 <span class="sub mono">报告/总报告.md${s.report_truncated ? '(界面截断 60KB)' : ''} · 唯一完整报告</span></h2><div class="pad">${md(s.report_md)}</div></div>` : '';
  const tlCard = s.timeline_md ? `<div class="card"><h2>时间线 <span class="sub mono">报告/时间线.md(追加式,新 → 旧)</span></h2><div class="pad">${md(s.timeline_md)}</div></div>` : '';
  /* 导图直接用 markmap 本体渲染:嵌的就是"独立窗口打开"的那个页面(同一份 思维导图.html,
     经 /api/shezha/mindmap 把 CDN 装载器换成本地 assets/markmap/,离线也能出图)。
     自己拿 SVG 画的版本是另一套观感,不如 markmap 原版好用 —— 已撤掉。 */
  const mmCard = (s.mm_md || s.mm_html) ? `<div class="card"><h2>思维导图 <span class="sub mono">报告/思维导图.html</span>
      <span class="right">${s.mm_html ? `<a class="pill" style="text-decoration:none" target="_blank" rel="noopener" href="/api/shezha/mindmap?domain=${encodeURIComponent(s.domain)}">新窗口打开 ↗</a>` : ''}</span></h2><div class="pad">
      <div class="row" style="margin-bottom:6px"><span class="t">${mmLine}${(s.mm_old && s.mm_old.length) ? ' · 旧版导图 ' + s.mm_old.length + ' 份(报告/导图_旧版/)' : ''}${mmTodo ? ' · <span style="color:var(--warn)">总报告已有内容,按规范该把结论增删进原图并重渲染</span>' : ''}</span></div>
      ${s.mm_md
        ? `<div class="mm-tools"><button onclick="mmView('full')">适应窗口</button><button onclick="mmView('100')">100%</button>
             <span class="t" id="mm_msg" style="font-size:11px;color:var(--text-faint)"></span></div>
           <svg class="mm-svg" id="mm_svg"></svg>
           <div class="mm-hint">滚轮缩放 · 拖拽平移 · 点节点展开 / 收起</div>`
        : '<div class="empty">还没有 报告/思维导图源文件.md(统一结构要求它存在)</div>'}
    </div></div>` : '';
  const invCard = `<div class="card"><h2>目录清点 <span class="sub">统一结构:00_档案 / 报告 / 证据(+ 可选 交付 · 大件存证)</span></h2><div class="pad"><div class="kv kv-wide">
      <div class="row"><span class="k">报告/</span><span class="v">${files(s.reports)}</span></div>
      <div class="row"><span class="k">证据/</span><span class="v">${s.evidence_n || 0} 份${(s.evidence && s.evidence.length) ? ' · 最近:' + files(s.evidence.slice(-6)) : ''}</span></div>
      <div class="row"><span class="k">交付/ · 大件存证/</span><span class="v">${s.deliver_n || 0} · ${s.big_n || 0} 份</span></div>
      <div class="row"><span class="k">哨位运行件</span><span class="v">${(s.stakeout && s.stakeout.length) ? files(s.stakeout) : '无'}</span></div>
      <div class="row"><span class="k">零散文件</span><span class="v">${(s.loose_other && s.loose_other.length) ? files(s.loose_other) : '无(结构已统一)'}</span></div>
    </div></div></div>`;
  $('view').innerHTML = `<div class="wrap">
    <div class="card"><div class="pad"><div class="qhead"><h1>${esc(s.domain)}</h1>
      <span class="pill">${esc(s.lib)}</span>${s.archived ? '<span class="pill">已归档</span>' : ''}${s.status ? `<span class="pill">${esc(s.status)}</span>` : ''}${live}</div>
      ${s.profile ? `<div class="desc">${esc(s.profile)}</div>` : ''}
      ${s.identity ? `<div class="kv kv-wide"><div class="row"><span class="k">实名线索</span><span class="v">${esc(s.identity)}</span></div></div>` : ''}
      <div class="row" style="margin-top:6px"><span class="t">关联:${esc(s.related || '—')}</span></div>
    </div></div>
    ${arcCard}
    ${rvCard}
    ${repCard}
    ${tlCard}
    ${mmCard}
    ${s.profile_md ? `<div class="card"><h2>${s.has_archive_md ? '站点档案 00_档案.md' : '旧布局报告补遗'} <span class="sub mono">${esc(s.archive)}</span></h2><div class="pad">${md(s.profile_md)}</div></div>` : ''}
    ${invCard}
    ${s.findings_md ? `<div class="card"><h2>FINDINGS</h2><div class="pad">${md(s.findings_md)}</div></div>` : ''}
    <div class="card"><h2>本站常驻脚本</h2><div class="pad">
      ${res ? `<table><thead><tr><th>名称</th><th>状态</th><th>用途</th><th>启动</th></tr></thead><tbody>${res}</tbody></table>`
            : '<div class="empty">该站暂无常驻脚本。</div>'}
    </div></div></div>`;
  if (s.mm_md) mountMindmap(s.mm_md);
}
/* 导图用 markmap 本体画在卡片里的 svg 上(渲染入口 markmapInto,完成后 fit 到可读比例)。
   画不出来要把话说明白 —— 空白导图最难查;工具条让人自己切「适应窗口 / 100%」。 */
let _mmHandle = null, _mmSvg = null;
function mmView(mode){
  if (_mmHandle && _mmSvg && window.markmapFit) window.markmapFit(_mmHandle, _mmSvg, mode === 'full' ? 'full' : '100');
}
function mountMindmap(src){
  const svg = $('mm_svg'), msg = $('mm_msg');
  if (!svg) return;
  if (!window.markmapInto){ if (msg) msg.textContent = 'markmap 库没加载(检查 /assets/markmap/ 脚本)'; return; }
  svg.innerHTML = '';
  window.markmapInto(svg, src).then(mm => { _mmHandle = mm; _mmSvg = svg; })
    .catch(e => { if (msg) msg.textContent = '导图渲染失败:' + ((e && e.message) || e); });
}
async function szDecide(domain, decision){
  const msg = document.getElementById('sz_dec_msg');
  if (msg) msg.textContent = '记录中…';
  try {
    const r = await post('/api/shezha', {action: 'review_decide', domain, decision});
    toast(r.message || '已记录', !r.ok);
    if (r.tree){ szTree = r.tree; szTreeAt = Date.now(); if (state && state.project === '涉诈批次') renderTasks(); }
    if (r.ok && szCur === domain) szPick(domain);      // 重载本卡,换成"已复核"状态
  } catch(e){ if (msg) msg.textContent = '失败:' + e; }
}
/* ---------------- 归档结案(单站 / 整簇 / 全部待审) ---------------- */
function szApplyTree(r){
  if (r.tree){ szTree = r.tree; szTreeAt = Date.now(); if (state && state.project === '涉诈批次') renderTasks(); }
}
function szClusterSites(name){
  const c = (((szTree || {}).clusters) || []).find(x => x.name === name);
  return (c && c.sites) || [];
}
function szLegitTodoN(){
  let n = 0;
  (((szTree || {}).clusters) || []).forEach(c => c.sites.forEach(s => {
    if (szIsArchived(s) || !szIsLegit(s)) return;
    if (!((s.review || {}).decision)) n++;
  }));
  return n;
}
async function szArchive(domain, reason){
  const box = document.getElementById('sz_arc_reason');
  const msg = document.getElementById('sz_arc_msg');
  const why = ((reason != null ? reason : ((box && box.value) || '')) + '').trim();
  if (msg) msg.textContent = '归档中…';
  try {
    const r = await post('/api/shezha', {action: 'archive', domain, reason: why});
    toast(r.message || '已归档', !r.ok);
    szApplyTree(r);
    if (r.ok && szCur === domain) szPick(domain);   // 重载本卡,换成"已归档 + 撤销"
  } catch(e){ if (msg) msg.textContent = '失败:' + e; }
}
async function szUnarchive(domain){
  const msg = document.getElementById('sz_arc_msg');
  if (msg) msg.textContent = '撤销中…';
  try {
    const r = await post('/api/shezha', {action: 'unarchive', domain});
    toast(r.message || '已撤销归档', !r.ok);
    szApplyTree(r);
    if (r.ok && szCur === domain) szPick(domain);
  } catch(e){ if (msg) msg.textContent = '失败:' + e; }
}
async function szArchiveCluster(){
  const name = _szClName;
  if (!name) return;
  const n = szClusterSites(name).filter(s => !szIsArchived(s)).length;
  if (!n) return;
  if (!confirm('把簇「' + name + '」里还没归档的 ' + n + ' 站全部归档结案?\\n归档 = 退出工作视图,派发侧也不再生成新目标;可逐个撤销。')) return;
  const msg = document.getElementById('sz_clai_msg');
  if (msg) msg.textContent = '整簇归档中…';
  try {
    const r = await post('/api/shezha', {action: 'archive_cluster', name});
    if (msg) msg.textContent = r.message;
    toast(r.message || '已归档', !r.ok);
    szApplyTree(r);
    if (r.ok) szPickCluster(name);
  } catch(e){ if (msg) msg.textContent = '失败:' + e; }
}
async function szArchivePending(){
  const n = szLegitTodoN();
  if (!n) return;
  if (!confirm('把这 ' + n + ' 个「待人工审核」的站全部归档结案?\\n即复核结论为"不是涉诈,不必再打" —— 退出工作视图且不再派发,可逐个撤销。')) return;
  szMsg('#sz_msg4', '归档中…');
  try {
    const r = await post('/api/shezha', {action: 'archive_pending'});
    szMsg('#sz_msg4', r.message);
    toast(r.message || '已归档', !r.ok);
    szApplyTree(r);
  } catch(e){ szMsg('#sz_msg4', '失败:' + e); }
}
async function szCluster(){
  szMsg('#sz_msg4', 'AI 归簇中(一次模型调用,约 1~2 分钟)…');
  try {
    const r = await post('/api/shezha', {action: 'ai_cluster'});
    szMsg('#sz_msg4', r.message);
    if (r.tree){ szTree = r.tree; szTreeAt = Date.now(); if (state && state.project === '涉诈批次') renderTasks(); }
  } catch(e){ szMsg('#sz_msg4', '失败:' + e); }
}
async function szSyncNow(){
  szMsg('#sz_msg5', '同步中(重建总库 → 增量归簇 → 增量评审,评审要读报告约 1~3 分钟)…');
  try {
    const r = await post('/api/shezha', {action: 'sync_now'});
    szMsg('#sz_msg5', (r.ok ? '' : '· ') + (r.message || ''));
    if (r.tree){ szTree = r.tree; szTreeAt = Date.now(); if (state && state.project === '涉诈批次') renderTasks(); }
    if (r.sync && szState){ szState.autosync = r.sync; renderSz(); }
    if (r.state){ szState = r.state; renderSz(); }
  } catch(e){ szMsg('#sz_msg5', '失败:' + e); }
}
async function szSyncToggle(){
  try {
    const r = await post('/api/shezha', {action: 'sync_toggle'});
    szMsg('#sz_msg5', r.message || '');
    const d = await get('/api/shezha'); szState = d; renderSz();
  } catch(e){ szMsg('#sz_msg5', '失败:' + e); }
}
async function szTimeline(days){
  szTlLoaded = true;
  try {
    const d = await get('/api/shezha/timeline?days=' + (days || 7));
    const mark = {data: '📦 数据战果', identity: '🪪 实名', deliver: '✅ 交付', finding: '🔎 发现',
                  batch: '📚 批次', lane: '🚗 车道', patrol: '🛡 巡检', weekly: '🗓 周报'};
    $('sz_tl').textContent = (d.events || []).map(e => `[${e.ts}] ${mark[e.kind] || '·'} ${e.site} ${e.text}`).join('\\n')
      || '区间内无事件';
  } catch(e){ $('sz_tl').textContent = '时间线加载失败:' + e; }
}
async function szWeekly(){
  szMsg('#sz_msg4', 'AI 周报生成中(后台,约 1~3 分钟,完成后自动刷新时间线)…');
  try {
    const r = await post('/api/shezha', {action: 'weekly'});
    szMsg('#sz_msg4', r.message);
  } catch(e){ szMsg('#sz_msg4', '失败:' + e); }
}
async function szPickCluster(name){
  _szClName = name; szCur = null; sel = null; qdata = null; persistViewState();
  $('view').innerHTML = '<div class="wrap"><div class="card"><div class="pad"><div class="empty">加载簇「' + esc(name) + '」…</div></div></div></div>';
  try {
    const c = await get('/api/shezha/cluster?name=' + encodeURIComponent(name));
    if (c.error){ _szClName = null; szCur = null; persistViewState(); renderSzTree(); $('view').innerHTML = '<div class="wrap"><div class="card"><div class="pad"><div class="empty">' + esc(c.error) + '</div></div></div></div>'; return; }
    renderSzCluster(c);
  } catch(e){ toast('簇加载失败:' + e, true); }
}
function renderSzCluster(c){
  const agg = c.agg || {};
  const DEGC = {deep: 'var(--success)', mid: 'var(--warn)', legit: 'var(--error-solid)', surface: 'var(--text-faint)', none: 'var(--text-faint)'};
  const DEG = {deep: ['扎实', 'ok'], mid: ['有获取', 'warn'], legit: ['待人工审核', 'bad'], surface: ['仅探测', ''], none: ['无材料', '']};
  const ECOL = {'同主域': '#4c8dff', '档案互指': '#b07cff', '实名同源': '#ff9f43'};
  const sitesByDomain = Object.fromEntries((c.sites || []).map(s => [s.domain, s]));
  const seen = new Set(), edges = [];
  for (const raw of [...(c.edges || []), ...((c.ai && c.ai.edges) || [])]){
    const a = raw.a, b = raw.b;
    if (!a || !b || a === b || !sitesByDomain[a] || !sitesByDomain[b]) continue;
    const ai = !!(c.ai && (c.ai.edges || []).some(x => x === raw));
    const source = ai ? 'AI' : '档案';
    const k = [a, b].sort().join('\\u0000') + '\\u0000' + source + '\\u0000' + (raw.type || '') + '\\u0000' + (raw.why || '');
    if (seen.has(k)) continue;
    seen.add(k); edges.push(Object.assign({}, raw, {a, b, ai, source}));
  }
  const parent = {}, find = x => {
    parent[x] = parent[x] || x;
    if (parent[x] !== x) parent[x] = find(parent[x]);
    return parent[x];
  };
  const union = (a, b) => { a = find(a); b = find(b); if (a !== b) parent[b] = a; };
  for (const s of c.sites || []) parent[s.domain] = s.domain;
  edges.forEach(e => union(e.a, e.b));
  const groups = {};
  for (const s of c.sites || []) (groups[find(s.domain)] = groups[find(s.domain)] || []).push(s);
  const compList = Object.values(groups).sort((a, b) => b.length - a.length);
  const escWhy = e => (e.why || e.type || '关系').trim();
  const short = d => d.length > 30 ? d.slice(0, 18) + '…' + d.slice(-9) : d;
  const edgeFor = (a, b) => edges.filter(e => (e.a === a && e.b === b) || (e.a === b && e.b === a));
  const compHtml = compList.map((members, ci) => {
    const names = new Set(members.map(s => s.domain));
    const localEdges = edges.filter(e => names.has(e.a) && names.has(e.b));
    const adj = Object.fromEntries(members.map(s => [s.domain, []]));
    localEdges.forEach(e => { adj[e.a].push(e); adj[e.b].push(e); });
    const root = [...members].sort((a, b) => (adj[b.domain].length - adj[a.domain].length) || a.domain.localeCompare(b.domain))[0].domain;
    const level = {[root]: 0}, order = [root], q = [root];
    while (q.length){
      const cur = q.shift();
      const next = adj[cur].map(e => e.a === cur ? e.b : e.a).sort();
      for (const n of next) if (!(n in level)){ level[n] = level[cur] + 1; q.push(n); order.push(n); }
    }
    const byLevel = {};
    order.forEach(n => (byLevel[level[n]] = byLevel[level[n]] || []).push(n));
    const pos = {};
    Object.entries(byLevel).forEach(([lv, ns]) => ns.forEach((n, i) => { pos[n] = {x: 20 + +lv * 268, y: 24 + i * 80}; }));
    const maxLevel = Math.max(...Object.keys(byLevel).map(Number), 0);
    const maxCount = Math.max(...Object.values(byLevel).map(ns => ns.length), 1);
    const width = Math.max(500, 20 + (maxLevel + 1) * 268);
    const height = Math.max(112, 24 + maxCount * 80);
    const treeEdges = [];
    for (const n of order.slice(1)){
      const candidates = adj[n].filter(e => {
        const other = e.a === n ? e.b : e.a;
        return level[other] === level[n] - 1;
      }).sort((a, b) => (a.ai ? 1 : 0) - (b.ai ? 1 : 0));
      if (candidates[0]) treeEdges.push(candidates[0]);
    }
    const paths = treeEdges.map(e => {
      const a = pos[e.a], b = pos[e.b]; if (!a || !b) return '';
      const left = level[e.a] < level[e.b] ? e.a : e.b, right = left === e.a ? e.b : e.a;
      const p1 = pos[left], p2 = pos[right], x1 = p1.x + 208, y1 = p1.y + 26, x2 = p2.x, y2 = p2.y + 26, mx = Math.round((x1 + x2) / 2);
      const col = e.ai ? 'var(--success)' : (ECOL[e.type] || 'var(--border-hover)');
      return `<path d="M${x1} ${y1} H${mx} V${y2} H${x2}" fill="none" stroke="${col}" stroke-width="1.8"${e.ai ? ' stroke-dasharray="6 4"' : ''} opacity=".9"/>`;
    }).join('');
    const cards = order.map(n => {
      const s = sitesByDomain[n], r = s.review || {}, d = DEG[r.degree] || DEG.surface;
      const sub = (r.got || s.profile || s.status || '').slice(0, 28);
      return `<div class="sz-comp-node sz-${r.degree || 'none'}" style="left:${pos[n].x}px;top:${pos[n].y}px" onclick="szPick('${esc(s.domain)}')" title="${esc(s.domain + (r.got ? (r.degree === 'legit' ? '｜疑似正规:' : '｜成果:') + r.got : ''))}"><b>${esc(short(s.domain))}</b><span><i class="sz-dot"></i>${d[0]}${sub ? ' · ' + esc(sub) : ''}</span></div>`;
    }).join('');
    const relKinds = [...new Set(localEdges.map(e => (e.ai ? 'AI·' : '') + (e.type || '关系')))];
    return `<section class="sz-comp"><div class="sz-comp-head"><b>分组 ${ci + 1}</b><span>${members.length} 站 · ${localEdges.length} 条关系</span><span class="sz-comp-kinds">${esc(relKinds.join(' / ') || '无直接关系')}</span></div><div class="sz-comp-body" style="height:${height}px;min-width:${width}px"><svg class="sz-comp-svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">${paths}</svg>${cards}</div></section>`;
  }).join('');
  const relationRows = edges.map((e, i) => `<tr><td>${i + 1}</td><td><span class="sz-rel-dot" style="background:${e.ai ? 'var(--success)' : (ECOL[e.type] || 'var(--text-faint)')}"></span>${esc(e.ai ? 'AI' : '档案')}</td><td>${esc(e.type || '关系')}</td><td class="mono">${esc(e.a)} ↔ ${esc(e.b)}</td><td>${esc(e.why || '—')}</td></tr>`).join('');
  const graphView = `<div id="sz_graph_view" class="sz-graph-view">${compHtml || '<div class="empty">簇内暂无站点</div>'}</div>`;
  const listView = `<div id="sz_rel_view" class="sz-rel-view" hidden>${edges.length ? `<table class="sz-rel-table"><thead><tr><th>#</th><th>来源</th><th>类型</th><th>站点</th><th>依据</th></tr></thead><tbody>${relationRows}</tbody></table>` : '<div class="empty">当前簇没有已记录的关系。可以点击右上角让 AI 补全。</div>'}</div>`;
  const legend = `<div class="legend sz-legend"><span><i style="background:#4c8dff"></i>同主域</span><span><i style="background:#b07cff"></i>档案互指</span><span><i style="background:#ff9f43"></i>实名同源</span><span><i style="background:var(--success)"></i>AI 关系(虚线)</span><span>左边框: <i style="background:var(--success)"></i>扎实 <i style="background:var(--warn)"></i>有获取 <i style="background:var(--error-solid)"></i>待人工审核 <i style="background:var(--text-faint)"></i>仅探测/无材料</span></div>`;
  const aiCard = c.ai ? `<div class="card"><h2>AI 簇综述 <span class="sub">${esc(c.ai.generated || '')}</span></h2><div class="pad"><div class="desc">${mdRender(c.ai.summary || '(空)')}</div></div></div>` : '';
const siteRows = (c.sites || []).map(s => {
    const r = s.review || {}, d = DEG[r.degree] || DEG.surface;
    const a = s.archived || {};
    const arcTag = s.archived ? ` <span class="pill" title="${esc('归档 ' + (a.at || '') + (a.reason ? ' · ' + a.reason : ''))}">已归档</span>` : '';
    return `<tr><td style="cursor:pointer" onclick="szPick('${esc(s.domain)}')"><b>${esc(s.domain)}</b>${arcTag}</td><td>${d[0] ? `<span class="pill ${d[1]}">${d[0]}</span>` : ''}</td><td class="t">${esc((r.got || '').slice(0, 46))}</td><td class="t">${esc((s.identity || '—').slice(0, 30))}</td><td class="t">${esc(s.status || '—')}</td></tr>`;
  }).join('');
  const nClArc = (c.sites || []).filter(s => s.archived).length;
  const arcClBtn = ((c.sites || []).length - nClArc)
    ? `<button onclick="szArchiveCluster()" title="整个簇查完了 / 不是涉诈:一次把簇内 ${(c.sites || []).length - nClArc} 站归档结案(可逐个撤销)">归档本簇 ${(c.sites || []).length - nClArc} 站</button>` : '';
  $('view').innerHTML = `<div class="wrap"><div class="card"><div class="pad"><div class="qhead"><h1>${esc(c.name)}</h1><span class="pill">${c.sites.length} 站</span>${agg.deep ? `<span class="pill ok">扎实 ${agg.deep}</span>` : ''}${agg.mid ? `<span class="pill warn">有获取 ${agg.mid}</span>` : ''}${agg.legit ? `<span class="pill bad">待人工审核 ${agg.legit}</span>` : ''}${nClArc ? `<span class="pill">已归档 ${nClArc}</span>` : ''}<span class="right">${arcClBtn}<button onclick="szClusterAI()">AI 补全簇关系分析</button><span class="t" id="sz_clai_msg" style="font-size:11.5px;color:var(--text-meta)"></span></span></div></div></div>
    <div class="card"><h2>簇关系图 <span class="sub">${compList.length} 个关系分组 · ${edges.length} 条关系 · 图上仅显示主干</span><span class="right"><button class="sz-tab active" id="sz_tab_graph" onclick="szSetGraphMode('graph')">图谱</button><button class="sz-tab" id="sz_tab_rel" onclick="szSetGraphMode('relations')">关系清单</button></span></h2><div class="pad">${graphView}${listView}${legend}</div></div>${aiCard}<div class="card"><h2>簇内站点 <span class="right">${c.sites.length} 站</span></h2><div class="pad"><table><thead><tr><th>站点</th><th>成果</th><th>拿到了什么</th><th>实名线索</th><th>状态</th></tr></thead><tbody>${siteRows}</tbody></table></div></div></div>`;
  szSetGraphMode(szGraphMode);
  beginViewRestore(); nudgeRestore();   // 新视图:只恢复一次,用户一滚就以用户为准
}
function szSetGraphMode(mode){
  szGraphMode = mode === 'relations' ? 'relations' : 'graph';
  const gv = $('sz_graph_view'), rv = $('sz_rel_view');
  if (gv) gv.hidden = szGraphMode !== 'graph';
  if (rv) rv.hidden = szGraphMode !== 'relations';
  const tg = $('sz_tab_graph'), tr = $('sz_tab_rel');
  if (tg) tg.classList.toggle('active', szGraphMode === 'graph');
  if (tr) tr.classList.toggle('active', szGraphMode === 'relations');
  persistViewState();
}
async function szClusterAI(){
  if (!_szClName) return;
  const msg = document.getElementById('sz_clai_msg');
  if (msg) msg.textContent = 'AI 通读簇内档案分析关系中…(约 30~60 秒)';
  try {
    const r = await post('/api/shezha', {action: 'cluster_ai', name: _szClName});
    if (msg) msg.textContent = r.message;
    if (r.ok) szPickCluster(_szClName);
  } catch(e){ if (msg) msg.textContent = '失败:' + e; }
}
async function szReview(){
  szMsg('#sz_msg4', 'AI 通读报告评审中(分批读 00_档案/报告/findings,约 10~20 分钟,树上逐步上色)…');
  try {
    const r = await post('/api/shezha', {action: 'review_ai'});
    szMsg('#sz_msg4', r.message);
  } catch(e){ szMsg('#sz_msg4', '失败:' + e); }
}
async function patrolNow(){
  const p = curProject;
  $('msg_patrol').textContent = '总Agent巡检中(只读材料 + 一次模型调用)…';
  try {
    const r = await post('/api/patrol', {action: 'start', project: p});
    $('msg_patrol').textContent = r.message;
    if (!r.ok) return;
    for (let i = 0; i < 60; i++){
      await new Promise(res => setTimeout(res, 3000));
      const st = await get('/api/patrol?project=' + encodeURIComponent(p));
      if (st.status && !st.status.running){
        $('msg_patrol').textContent = (st.status.rc === 0 ? '巡检完成 ' : '巡检失败 ') + (st.status.finished || '');
        if (st.last) $('patrol_out').innerHTML = mdRender(st.last.content);
        return;
      }
    }
    $('msg_patrol').textContent = '巡检仍在后台进行,稍后再点一次查看';
  } catch(e){ $('msg_patrol').textContent = '失败:' + e; }
}

/* ---------------- 轮询 ---------------- */
async function tick(){
  const seq = ++tickSeq, project = curProject;
  try {
    const d = await get('/api/state?project=' + encodeURIComponent(project));
    if (seq !== tickSeq || project !== curProject || d.project !== curProject) return;
    state = d;
    renderProjects(); renderHeader(); renderTasks(); renderProjectList();
    loadAiUsage();
    await restoreViewLocation();
    if (seq !== tickSeq || project !== curProject) return;
    if (sel){ await loadQuestion(false); if (qdata) renderTask(); }
    scheduleRestore();
    nudgeRestore();
  } catch (e){
    // 不要静默吞异常:前端一旦出错,页面会停在「等待数据…」而毫无提示 —— 实测踩过:
    // 模板里一个换行转义写错就让整个 script 语法错误、页面全白,排查了很久。至少让人看见。
    const t = document.getElementById('tasks');
    if (t && !t.dataset.err){
      t.dataset.err = '1';
      t.innerHTML = '<div class="empty">控制台渲染出错:' + esc(String(e)).slice(0, 200) + '</div>';
    }
  }
}
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k'){ e.preventDefault(); $('q').focus(); }
  if (e.key === 'Escape') closeDrawer();
});
tick();
/* 标签页不可见时停掉轮询:后台标签没必要每 2s 重建一次 DOM,回来时立刻补一轮。 */
setInterval(() => { if (!document.hidden) tick(); }, 2000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj_or_bytes, ctype: str, headers=()) -> None:
        body = (obj_or_bytes if isinstance(obj_or_bytes, bytes)
                else json.dumps(obj_or_bytes, ensure_ascii=False).encode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:                      # 静态资源可带缓存头(字体这类)
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端提前断开(轮询超时/关页面/刷新)—— 正常现象,不要让它把请求线程的堆栈刷进日志
            # (2026-10-19:字体资源被中断时刷了一屏 traceback,把控制台日志埋了)
            pass

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        J = "application/json; charset=utf-8"

        def one(k: str, default: str = "") -> str:
            return (qs.get(k) or [default])[0]

        if path == "/api/ai_usage":
            pr = find_project(one("project", DEFAULT_PROJECT)) or find_project(DEFAULT_PROJECT)
            data = read_json(Path(pr["dir"]) / "logs" / "ai_usage.json", {})
            data["available"] = bool(data.get("generated_at"))
            data["project"] = pr["name"]
            self._send(data, J)
        elif path == "/api/state":
            self._send(build_state(one("project", DEFAULT_PROJECT)), J)
        elif path == "/api/question":
            # 单题详情:一次把这道题要用的全备齐(以前是三个接口各自"算全部题",题一多就卡)
            pr = find_project(one("project", DEFAULT_PROJECT)) or find_project(DEFAULT_PROJECT)
            rundir = Path(pr["dir"])
            qid = one("qid")
            board = read_json(rundir / "board.json", {"questions": {}})
            entry = (board.get("questions") or {}).get(qid)
            if not entry:
                self._send({"error": "no such question", "qid": qid}, J)
            else:
                data = build_question(rundir, entry)
                data["question"] = entry
                data["subs"] = question_subs(rundir, qid)
                if pr["name"] == SHEZHA_PROJECT:   # 涉诈站点:附档案视图(档案/常驻/报告/证据)
                    data["site"] = shezha_site_payload(entry)
                self._send(data, J)
        elif path == "/api/shezha/residents":
            self._send(shezha_residents(one("site") or None), J)
        elif path == "/api/shezha/tree":
            self._send(shezha_cluster_view(force=one("force") == "1"), J)
        elif path == "/api/shezha/cluster":
            data = shezha_cluster_detail(unquote(one("name")))
            self._send(data if data else {"error": "no such cluster", "name": one("name")}, J)
        elif path == "/api/shezha/site":
            data = shezha_site_view(one("domain"))
            self._send(data if data else {"error": "no such site", "domain": one("domain")}, J)
        elif path == "/api/shezha/mindmap":
            # 站点思维导图 html(只读):路径从总库条目拼,不接受调用方给路径;找不到给个说明页
            mp = shezha_mindmap_path(one("domain"))
            if not mp:
                self._send("<html><body style='font:14px system-ui;padding:2em'>"
                           "这个站点还没有导图(或站点不存在)。统一结构:报告/思维导图.html</body></html>"
                           .encode("utf-8"), "text/html; charset=utf-8")
            else:
                try:
                    html = mp.read_text(encoding="utf-8", errors="ignore")
                    self._send(_mindmap_localize(html).encode("utf-8"), "text/html; charset=utf-8")
                except OSError as e:
                    self._send({"error": f"读取导图失败: {e}"}, J)
        elif path == "/api/shezha/timeline":
            self._send({"days": int(one("days", "7") or 7),
                        "events": shezha_timeline(days=int(one("days", "7") or 7))}, J)
        elif path == "/api/patrol":
            self._send(patrol_status(one("project", DEFAULT_PROJECT)), J)
        elif path == "/api/providers":
            self._send(provider_list(), J)
        elif path == "/api/knowledge":
            self._send({"items": list_knowledge()}, J)
        elif path.startswith("/api/knowledge/"):
            rel = unquote(path[len("/api/knowledge/"):])
            self._send({"path": rel, "content": read_knowledge(rel)}, J)
        elif path == "/api/kb-search":
            self._send({"query": one("q"), "results": search_kb(one("q"))}, J)
        elif path == "/api/shezha":
            st = shezha_state()
            st["residents"] = shezha_residents()
            self._send(st, J)
        elif path.startswith("/assets/"):
            self._serve_asset(unquote(path[len("/assets/"):]))   # 字体等静态资源(Hermes 风格的字体)
        elif path in ("/", "/index.html"):
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def _serve_asset(self, rel: str) -> None:
        """托管 assets/ 里的静态资源(目前只有 Hermes 风格的字体);只允许读该目录,防路径穿越。"""
        base = (ROOT / "assets").resolve()
        target = (base / rel).resolve()
        if base not in target.parents and target != base:
            self.send_error(403)
            return
        if not target.is_file():
            self.send_error(404)
            return
        ctype = {"woff2": "font/woff2", "woff": "font/woff", "css": "text/css",
                 "js": "application/javascript", "png": "image/png", "svg": "image/svg+xml"}.get(
            target.suffix.lstrip("."), "application/octet-stream")
        cache = "no-store" if target.suffix in (".js", ".css") else "public, max-age=86400"
        self._send(target.read_bytes(), ctype, headers=(("Cache-Control", cache),))

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        J = "application/json; charset=utf-8"
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            payload = {}
        if path == "/api/config":
            msg = save_config(payload)
            self._send({"ok": "失败" not in msg, "message": msg}, "application/json; charset=utf-8")
        elif path == "/api/test-contest":
            msg = test_contest()
            self._send({"ok": msg.startswith("接口正常"), "message": msg},
                       "application/json; charset=utf-8")
        elif path == "/api/test-vpn":
            msg = test_vpn()
            self._send({"ok": "VPN 已连通" in msg or "不需要 VPN 预检" in msg or "未启用 VPN 预检" in msg,
                        "message": msg},
                       "application/json; charset=utf-8")
        elif path == "/api/test-model":
            msg = test_model()
            self._send({"ok": msg.startswith("模型连通正常"), "message": msg},
                       "application/json; charset=utf-8")
        elif path == "/api/runner":
            action = (payload.get("action") or "").lower()
            project = payload.get("project") or DEFAULT_PROJECT
            if project == SHEZHA_PROJECT:      # 涉诈批次:走 shezha 专用启动(预检+env 注入)
                if action == "start":
                    msg = shezha_runner("start")
                elif action == "stop":
                    msg = shezha_runner("stop")
                elif action == "restart":
                    msg = shezha_runner("stop") + " / " + shezha_runner("start")
                else:
                    msg = f"未知操作: {action}"
            elif action == "start":
                msg = start_runner(project)
            elif action == "stop":
                msg = stop_runner(project)
            elif action == "restart":
                msg = stop_runner(project) + " / " + start_runner(project)
            else:
                msg = f"未知操作: {action}"
            self._send({"ok": "失败" not in msg and "未知" not in msg, "message": msg},
                       "application/json; charset=utf-8")
        elif path == "/api/projects":
            action = (payload.get("action") or "").lower()
            if action == "create":
                msg = create_project(payload.get("name") or "", payload.get("profile") or "")
                self._send({"ok": msg.startswith("已创建"), "message": msg,
                            "projects": list_projects()}, "application/json; charset=utf-8")
            elif action == "delete":
                msg = delete_project(payload.get("name") or "")
                self._send({"ok": msg.startswith("已删除"), "message": msg,
                            "projects": list_projects()}, "application/json; charset=utf-8")
            else:
                self._send({"ok": True, "projects": list_projects()}, "application/json; charset=utf-8")
        elif path == "/api/providers":
            msg = provider_action(payload)
            self._send({"ok": not msg.startswith("✘") and "未知" not in msg, "message": msg,
                        "providers": provider_list()}, J)
        elif path == "/api/shezha":
            action = (payload.get("action") or "").lower()
            if action == "config":
                msg = shezha_config(payload)
                self._send({"ok": not msg.startswith("✘"), "message": msg,
                            "state": shezha_state()}, J)
            elif action == "excel":
                err, out, meta = shezha_excel(payload)
                self._send({"ok": err == "", "message": err or "切批次完成", "output": out,
                            "used_agent": meta.get("used_agent", False),
                            "mapping": meta.get("mapping"),
                            "state": shezha_state()}, J)
            elif action == "runner":
                msg = shezha_runner((payload.get("runner_action") or "start").lower())
                self._send({"ok": "失败" not in msg and "未在运行" not in msg and "为空" not in msg,
                            "message": msg, "state": shezha_state()}, J)
            elif action == "close":
                msg = shezha_close(int(payload.get("batch") or 0), bool(payload.get("scribe")))
                self._send({"ok": "已开始" in msg, "message": msg, "state": shezha_state()}, J)
            elif action == "ai_cluster":
                msg = shezha_ai_cluster()
                self._send({"ok": msg.startswith("AI 归簇完成") or "无需" in msg, "message": msg,
                            "tree": shezha_cluster_view(force=True)}, J)
            elif action == "cluster_ai":
                msg = shezha_cluster_ai(str(payload.get("name") or ""))
                self._send({"ok": msg.startswith("簇关系 AI 分析完成"), "message": msg}, J)
            elif action == "review_ai":
                msg = shezha_review_ai()
                self._send({"ok": "已开始" in msg, "message": msg, "state": shezha_state()}, J)
            elif action == "review_decide":
                msg = shezha_review_decide(str(payload.get("domain") or ""),
                                           str(payload.get("decision") or ""))
                self._send({"ok": msg.startswith("已") or "记入停打名单" in msg, "message": msg,
                            "tree": shezha_cluster_view(force=True)}, J)
            elif action == "archive":
                msg = shezha_archive_add([str(payload.get("domain") or "")],
                                         str(payload.get("reason") or ""))
                self._send({"ok": not msg.startswith("✘"), "message": msg,
                            "tree": shezha_cluster_view(force=True)}, J)
            elif action == "unarchive":
                msg = shezha_unarchive(str(payload.get("domain") or ""))
                self._send({"ok": not msg.startswith("✘"), "message": msg,
                            "tree": shezha_cluster_view(force=True)}, J)
            elif action == "archive_cluster":
                msg = shezha_archive_cluster(str(payload.get("name") or ""))
                self._send({"ok": not msg.startswith("✘"), "message": msg,
                            "tree": shezha_cluster_view(force=True)}, J)
            elif action == "archive_pending":
                msg = shezha_archive_pending()
                self._send({"ok": not msg.startswith("✘"), "message": msg,
                            "tree": shezha_cluster_view(force=True)}, J)
            elif action == "sync_now":
                r = shezha_autosync_tick(force=True)
                self._send({"ok": bool(r.get("ran")), "message":
                            "同步完成:" + "; ".join(r.get("actions") or [r.get("why", "")])
                            if r.get("ran") else ("立即同步:" + r.get("why", "")),
                            "sync": _sz_sync_state(Path(shezha_state().get("workdir") or "/tmp")),
                            "tree": shezha_cluster_view(force=True)}, J)
            elif action == "sync_toggle":
                on = str((_shezha_cfg().get("SHEZHA_AUTOSYNC", "1"))).strip() in ("0", "off", "false")
                config_mod.write_env({"SHEZHA_AUTOSYNC": "0" if on else "1"})
                apply_env()
                self._send({"ok": True,
                            "message": ("自动同步已关闭" if on else "自动同步已开启,"
                                        "每 %d 秒扫一次仓库,新料自动跟上" % SZ_SYNC_TICK)}, J)
            elif action == "weekly":
                msg = shezha_weekly()
                self._send({"ok": "已开始" in msg, "message": msg}, J)
            else:
                self._send({"ok": False, "message": f"未知操作: {action}",
                            "state": shezha_state()}, J)
        elif path == "/api/patrol":
            action = (payload.get("action") or "start").lower()
            project = payload.get("project") or DEFAULT_PROJECT
            if action == "start":
                msg = patrol(project)
                self._send({"ok": "已开始" in msg, "message": msg,
                            "patrol": patrol_status(project)}, J)
            else:
                self._send({"ok": False, "message": f"未知操作: {action}"}, J)
        else:
            self.send_error(404)

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799, help="监听端口(被占用时自动往后试)")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    srv = None
    for port in range(args.port, args.port + 50):  # 本机常有别的服务占端口,自动顺延
        try:
            srv = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            print(f"端口 {port} 被占用,尝试 {port + 1} …")
    if srv is None:
        raise SystemExit("找不到可用端口")
    port = srv.server_address[1]
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "dashboard.port").write_text(str(port), encoding="utf-8")
    print(f"控制台已启动: http://{args.host}:{port}  (Ctrl+C 停止)")
    _start_autosync()      # 涉诈自动同步:仓库新料 → 总库/归簇/评审自动跟上
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
