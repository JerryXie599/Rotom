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
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import config as config_mod  # noqa: E402

PROJECTS_DIR = ROOT / "projects"
DEFAULT_PROJECT = "默认项目"

STATUS_LABEL = {"pending": "待派发", "running": "运行中", "solved": "已解出", "failed": "已放弃"}
STATUS_CLASS = {"pending": "warn", "running": "run", "solved": "ok", "failed": "bad"}

EDITABLE_KEYS = ("TEAM_TOKEN", "PI_BASE_URL", "PI_API_KEY", "PI_MODEL", "PI_API_TYPE",
                 "CONTEST_BASE", "CONTEST_QUERY_PATH", "CONTEST_RESET_PATH", "CONTEST_SUBMIT_PATH",
                 "START_WORKERS", "IGNORE_SOLVED")
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
            "builtin": True, "questions": _count_questions(ROOT)}]
    if PROJECTS_DIR.exists():
        for d in sorted(PROJECTS_DIR.iterdir()):
            if not d.is_dir():
                continue
            meta = read_json(d / "meta.json", {})
            out.append({"name": meta.get("name") or d.name, "dir": str(d),
                        "created": meta.get("created"), "builtin": False,
                        "questions": _count_questions(d)})
    return out


def _count_questions(rundir: Path) -> int:
    board = read_json(rundir / "board.json", {})
    return len(board.get("questions") or {})


def find_project(name: str) -> dict | None:
    for pr in list_projects():
        if pr["name"] == name:
            return pr
    return None


def create_project(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "项目名不能为空"
    if name == DEFAULT_PROJECT or find_project(name):
        return f"项目已存在: {name}"
    d = project_dir(name)
    if d.exists() and any(d.iterdir()):
        return f"目录已存在且非空: {d}"
    for sub in ("work", "logs"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps({"name": name, "created": time.strftime("%F %T")}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return f"已创建项目「{name}」→ {d}"


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
    running = bool(pid and pid_alive(pid))
    if pid_file.exists() and not running:
        pid_file.unlink(missing_ok=True)
    return {"running": running, "pid": pid}


def start_runner(project: str) -> str:
    pr = find_project(project)
    if not pr:
        return f"项目不存在: {project}"
    rundir = Path(pr["dir"])
    st = runner_status(rundir)
    if st["running"]:
        return f"「{project}」的 runner 已在运行 (pid {st['pid']})"
    env = {**os.environ, "WQH_RUN_DIR": str(rundir)}  # 数据目录指向本项目,项目之间互不干扰
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


def save_config(payload: dict) -> str:
    updates = {}
    for k in EDITABLE_KEYS:
        if k in payload and payload[k] is not None:
            updates[k] = str(payload[k]).strip()
    if "PI_BASE_URL" in updates:
        updates["PI_BASE_URL"] = updates["PI_BASE_URL"].rstrip("/")
    if "PI_API_TYPE" in updates and updates["PI_API_TYPE"] not in API_TYPES:
        return f"未知 API 类型: {updates['PI_API_TYPE']}"
    if not updates:
        return "没有可保存的字段"
    config_mod.write_env(updates)
    apply_env()  # 让本进程立即用上新地址
    cfg = {**config_mod.DEFAULTS, **config_mod.read_env()}
    ok = config_mod.sync_provider(cfg, quiet=True)
    mask = {k: ("***" if ("KEY" in k or "TOKEN" in k) else v) for k, v in updates.items()}
    msg = f"已保存: {mask}"
    if ok:
        msg += "; 已同步到 pi provider(wqh)"
    else:
        msg += "; 未同步 provider(缺 base URL 或模型名)"
    running_projects = [pr["name"] for pr in list_projects()
                        if runner_status(Path(pr["dir"]))["running"]]
    if running_projects:
        msg += f"。注意:这些项目的 runner 正在跑,模型/token 变更需重启才生效 → {'、'.join(running_projects)}"
    return msg


def test_contest() -> str:
    """用当前配置实测比赛接口:能不能查到题、能查到几道。"""
    import contest_api
    apply_env()
    token = (config_mod.read_env().get("TEAM_TOKEN") or "").strip()
    if not token:
        return "未配置队伍 token"
    try:
        qs = contest_api.list_questions(token, timeout=20)
    except Exception as e:
        return f"接口不通: {str(e)[:220]}"
    if not qs:
        return "接口通了,但题目列表为空(可能比赛未开始或 token 无队伍信息)"
    titles = ", ".join(str(q.get("title")) for q in qs[:6])
    return f"接口正常:拉到 {len(qs)} 道题({titles}{'…' if len(qs) > 6 else ''})"


def test_model() -> str:
    cfg = {**config_mod.DEFAULTS, **config_mod.read_env()}
    model = cfg.get("PI_MODEL") or ""
    if not model:
        return "未配置模型名"
    config_mod.sync_provider(cfg, quiet=True)
    env = {**os.environ}
    if cfg.get("PI_API_KEY_VAR") and cfg.get("PI_API_KEY"):
        env[cfg["PI_API_KEY_VAR"]] = cfg["PI_API_KEY"]
    cmd = ["pi", "-p", "--provider", cfg.get("PI_PROVIDER") or "wqh", "--model", model,
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
            return p.read_text(encoding="utf-8", errors="ignore")[-2500:]
    return ""


def build_state(project: str = DEFAULT_PROJECT) -> dict:
    pr = find_project(project) or find_project(DEFAULT_PROJECT)
    project = pr["name"]
    rundir, logs = Path(pr["dir"]), Path(pr["dir"]) / "logs"
    board = read_json(rundir / "board.json", {"questions": {}, "facts": []})
    reg = [w for w in read_json(logs / "workers.json", []) if pid_alive(w.get("pid"))]
    runner_running = runner_status(rundir)["running"]
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
            "elapsed": int(time.time() - started) if started and e.get("status") == "running" else None,
            "connection": e.get("connection") or {}, "workdir": e.get("workdir") or "",
            "description": (e.get("description") or "")[:200],
            "workers": acts, "shared": shared_board(rundir, e),
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
    return {
        "now": time.strftime("%H:%M:%S"),
        "solved": sum(1 for q in questions if q["status"] == "solved"), "total": len(questions),
        "running_workers": len(reg), "paused": bool(rstate.get("paused")),
        "limit": rstate.get("limit"), "runner": runner_status(rundir),
        "project": project, "projects": list_projects(),
        "config": {**config_mod.DEFAULTS, **config_mod.read_env()},
        "facts": list(reversed(board.get("facts", [])[-25:])),
        "questions": questions, "submissions": subs,
    }


# ---------------------------------------------------------------- 页面

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>wqh agent 控制台</title>
<style>
/* ---- 设计 token:深色为默认,浅色为 body.light(点右上角按钮切换,选择记在 localStorage) ---- */
:root{
  --bg:#0a0a0a; --surface:#111113; --surface-2:#171717; --surface-3:#1d1d20;
  --border:#242424; --border-hover:#3a3a3a;
  --text:#fafafa; --text-body:#b3b3b3; --text-meta:#8a8a8a; --text-faint:#666;
  --accent:#4f46e5; --accent-soft:rgba(79,70,229,.15); --accent-text:#c7d2fe;
  --success:#34d399; --success-bg:rgba(16,185,129,.10); --success-bd:rgba(16,185,129,.40);
  --error:#fda4af; --error-bg:rgba(244,63,94,.10); --error-bd:rgba(244,63,94,.40); --error-solid:#dc2626;
  --warn:#fcd34d; --warn-bg:rgba(245,158,11,.10); --warn-bd:rgba(245,158,11,.40);
  --header-bg:rgba(10,10,10,.93); --hover:rgba(255,255,255,.045); --pill-bg:rgba(255,255,255,.04);
  --radius:8px; --radius-sm:6px;
  --mono:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;
}
/* 浅色主题:白底 + 灰边,层级同样靠边框而非阴影 */
body.light{
  --bg:#f6f7f9; --surface:#ffffff; --surface-2:#f2f4f7; --surface-3:#e9ecf1;
  --border:#e3e6eb; --border-hover:#c8cdd6;
  --text:#0f172a; --text-body:#334155; --text-meta:#64748b; --text-faint:#94a3b8;
  --accent:#4f46e5; --accent-soft:rgba(79,70,229,.10); --accent-text:#4338ca;
  --success:#047857; --success-bg:rgba(5,150,105,.10); --success-bd:rgba(5,150,105,.35);
  --error:#b91c1c; --error-bg:rgba(220,38,38,.08); --error-bd:rgba(220,38,38,.30); --error-solid:#dc2626;
  --warn:#b45309; --warn-bg:rgba(217,119,6,.12); --warn-bd:rgba(217,119,6,.35);
  --header-bg:rgba(255,255,255,.92); --hover:rgba(15,23,42,.045); --pill-bg:rgba(15,23,42,.04);
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text-body);overflow:hidden;
  font:13px/1.5 system-ui,-apple-system,"PingFang SC","Segoe UI",Roboto,sans-serif;
  display:grid;grid-template-columns:236px 1fr;transition:grid-template-columns .15s}
body.collapsed{grid-template-columns:56px 1fr}
b,strong{font-weight:600}
.num{font-variant-numeric:tabular-nums}

/* ---- 侧栏 ---- */
aside{background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;
  min-width:0;overflow:hidden}
.brand{height:56px;flex:0 0 56px;display:flex;align-items:center;gap:10px;padding:0 14px;
  border-bottom:1px solid var(--border);white-space:nowrap;overflow:hidden}
.brand .dot{width:9px;height:9px;border-radius:50%;background:var(--accent);flex:0 0 9px}
.brand .name{font-size:14px;font-weight:600;color:var(--text)}
nav{padding:8px;display:flex;flex-direction:column;gap:2px}
.nav-item{display:flex;align-items:center;gap:10px;height:34px;padding:0 10px;border-radius:var(--radius-sm);
  color:var(--text-meta);cursor:pointer;white-space:nowrap;overflow:hidden;
  transition:background .15s,color .15s}
.nav-item:hover{background:var(--hover);color:var(--text-body)}
.nav-item.active{background:var(--accent-soft);color:var(--accent-text)}
.nav-item .ico{flex:0 0 16px;display:flex;justify-content:center}
.side-foot{margin-top:auto;padding:10px 14px;border-top:1px solid var(--border);
  font-size:11px;color:var(--text-faint);white-space:nowrap;overflow:hidden}
body.collapsed .name,body.collapsed .nav-item span,body.collapsed .side-foot{display:none}
body.collapsed .nav-item{justify-content:center;padding:0}

/* ---- 主区 ---- */
.col{display:flex;flex-direction:column;min-width:0;height:100vh}
header{height:56px;flex:0 0 56px;border-bottom:1px solid var(--border);display:flex;align-items:center;
  gap:14px;padding:0 16px;background:var(--bg)}
header h1{font-size:15px;margin:0;color:var(--text);font-weight:600;white-space:nowrap}
.hstat{font-size:12px;color:var(--text-meta);white-space:nowrap}
.hstat b{color:var(--text);font-size:13px}
.spacer{flex:1}
main{flex:1;overflow:auto;padding:16px;min-height:0}
.view{display:none}.view.active{display:block}

/* ---- KPI ---- */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:14px}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px}
.kpi .k{font-size:11px;color:var(--text-meta);letter-spacing:.02em;text-transform:uppercase}
.kpi .v{font-size:22px;font-weight:600;color:var(--text);margin-top:4px;line-height:1.2}
.kpi .s{font-size:11px;color:var(--text-faint);margin-top:2px}

/* ---- 卡片 / pill ---- */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px}
.card h2{margin:0 0 12px;font-size:13px;font-weight:600;color:var(--text);letter-spacing:.02em;
  display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;line-height:1;
  padding:3px 8px;border-radius:4px;border:1px solid var(--border);color:var(--text-meta);
  background:var(--pill-bg);white-space:nowrap}
.pill.run{color:var(--success);background:var(--success-bg);border-color:var(--success-bd)}
.pill.err{color:var(--error);background:var(--error-bg);border-color:var(--error-bd)}
.pill.warn{color:var(--warn);background:var(--warn-bg);border-color:var(--warn-bd)}
.pill.ok{color:var(--success);background:var(--success-bg);border-color:var(--success-bd)}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:0 0 7px}
.pill.run .dot{animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}

/* ---- 工具条 ---- */
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
input[type=search],input[type=text],select{background:var(--surface-2);border:1px solid var(--border);
  color:var(--text);border-radius:var(--radius-sm);height:34px;padding:0 10px;font-size:13px;font-family:inherit}
input:focus,select:focus{outline:none;border-color:var(--accent)}
input[type=search]{width:220px}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;line-height:1;padding:5px 11px;
  border-radius:999px;border:1px solid var(--border);background:var(--surface-2);color:var(--text-meta);
  cursor:pointer;transition:background .15s,border-color .15s,color .15s}
.chip:hover{border-color:var(--border-hover);color:var(--text-body)}
.chip.active{background:var(--accent-soft);border-color:var(--accent);color:var(--accent-text)}

/* ---- master-detail ---- */
.split{display:grid;grid-template-columns:300px minmax(0,1fr);gap:1px;background:var(--border);
  border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;min-height:420px}
.list{background:var(--surface);overflow:auto;max-height:calc(100vh - 260px)}
.row{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:9px;height:46px;
  padding:0 12px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .15s}
.row:hover{background:var(--hover)}
.row.sel{background:var(--accent-soft);box-shadow:inset 2px 0 0 var(--accent)}
.row .t{min-width:0}
.row .t .n{font-size:13px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .t .m{font-size:11px;color:var(--text-faint);font-family:var(--mono)}
.detail{background:var(--surface);padding:16px;overflow:auto;max-height:calc(100vh - 260px);min-width:0}
.detail .hd{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.detail h3{margin:0;font-size:16px;font-weight:600;color:var(--text)}
.meta{font-size:12px;color:var(--text-meta);margin:4px 0}
.meta code{font-family:var(--mono);font-size:11px;color:var(--text-body);
  background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:1px 5px}
.sect{margin-top:14px;border-top:1px solid var(--border);padding-top:12px}
.sect .lbl{font-size:11px;color:var(--text-meta);letter-spacing:.02em;text-transform:uppercase;margin-bottom:8px}
.wk{margin-bottom:10px}
.wk .wh{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text-body);
  font-family:var(--mono);margin-bottom:6px}
.act{font-family:var(--mono);font-size:11.5px;color:var(--text-body);background:var(--surface-2);
  border:1px solid var(--border);border-left:2px solid var(--border-hover);border-radius:4px;
  padding:6px 8px;margin:4px 0;white-space:pre-wrap;word-break:break-all}
.act.say{border-left-color:var(--accent);color:var(--text)}
.act.err{border-left-color:var(--error-solid);color:var(--error)}
pre{margin:0;white-space:pre-wrap;word-break:break-word;background:var(--surface-2);
  border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px;font-size:11.5px;
  font-family:var(--mono);color:var(--text-body);max-height:260px;overflow:auto}
details summary{cursor:pointer;color:var(--text-meta);font-size:12px;padding:4px 0}
details summary:hover{color:var(--text-body)}

/* ---- 表单 ---- */
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px 14px}
.f label{display:block;font-size:11px;color:var(--text-meta);margin-bottom:5px;letter-spacing:.02em}
.f input,.f select{width:100%;font-family:var(--mono);font-size:12px}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}
button{background:var(--surface-3);color:var(--text-body);border:1px solid var(--border);
  border-radius:var(--radius-sm);height:34px;padding:0 14px;font-size:13px;font-family:inherit;
  cursor:pointer;transition:background .15s,border-color .15s,color .15s}
button:hover{border-color:var(--border-hover);color:var(--text)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.primary:hover{background:#4338ca}
button.danger{color:var(--error);border-color:var(--error-bd)}
button.danger:hover{background:var(--error-bg)}
#msg{font-size:12px;color:var(--success);margin-left:4px;align-self:center}
#msg.err{color:var(--error)}
.hint{font-size:12px;color:var(--text-meta);line-height:1.7}
.hint b{color:var(--text-body)}

/* ---- 表格 / 事件 ---- */
table{width:100%;border-collapse:collapse;font-size:12px}
th{background:var(--surface-2);color:var(--text-meta);font-weight:600;font-size:11px;text-align:left;
  padding:8px 10px;border-bottom:1px solid var(--border);position:sticky;top:0}
td{padding:7px 10px;border-bottom:1px solid var(--border);color:var(--text-body);font-family:var(--mono);
  font-size:11.5px;word-break:break-all}
td.q{font-family:inherit;font-size:12px;color:var(--text)}
.ev{display:flex;gap:10px;padding:6px 2px;border-bottom:1px solid var(--border);font-size:12px}
.ev .ts{font-family:var(--mono);font-size:11px;color:var(--text-faint);flex:0 0 62px}
.ev .src{color:var(--accent-text);flex:0 0 70px;font-size:11px}
.ev .tx{color:var(--text-body);min-width:0;word-break:break-word}
.empty{color:var(--text-faint);text-align:center;padding:34px 0;font-size:12px}
@media(max-width:900px){body{grid-template-columns:56px 1fr}.name,.nav-item span,.side-foot{display:none}
  .split{grid-template-columns:1fr}.list{max-height:260px}}
</style></head><body>
<aside>
  <div class="brand"><span class="dot"></span><span class="name">wqh agent 控制台</span></div>
  <nav>
    <div class="nav-item active" data-view="overview" onclick="go('overview')">
      <span class="ico">▤</span><span>总览</span></div>
    <div class="nav-item" data-view="control" onclick="go('control')">
      <span class="ico">⚙</span><span>控制台</span></div>
    <div class="nav-item" data-view="records" onclick="go('records')">
      <span class="ico">≡</span><span>记录</span></div>
  </nav>
  <div class="side-foot"><span id="sfoot">-</span></div>
</aside>
<div class="col">
  <header>
    <h1 id="viewtitle">总览</h1>
    <span class="hstat">进度 <b id="k_prog">-</b></span>
    <span class="hstat">在跑 agent <b id="k_work">-</b></span>
    <span class="hstat">runner <b id="k_run">-</b></span>
    <span class="hstat" id="k_pause"></span>
    <span class="hstat">项目 <b id="k_proj">-</b></span>
    <span class="spacer"></span>
    <button id="themebtn" onclick="toggleTheme()" title="切换浅色/深色主题">🌙 深色</button>
    <button onclick="document.body.classList.toggle('collapsed')" title="折叠侧栏">☰</button>
  </header>
  <main>
    <!-- 总览 -->
    <div class="view active" id="v-overview">
      <div class="card" style="margin-bottom:14px">
        <h2>项目 <span class="pill" id="proj_pill">-</span></h2>
        <div class="form" style="grid-template-columns:minmax(220px,1.2fr) minmax(220px,1.2fr) auto;align-items:end">
          <div class="f"><label>当前项目(每个项目有独立的工作区与状态,互不干扰)</label>
            <select id="proj" onchange="switchProject(this.value)"></select></div>
          <div class="f"><label>新建项目</label>
            <input type="text" id="newproj" placeholder="例如 round1、练习-多题" onkeydown="if(event.key==='Enter')createProject()"></div>
          <div class="btns" style="margin:0">
            <button onclick="createProject()">创建项目</button>
          </div>
        </div>
        <div class="btns">
          <button class="primary" onclick="act('start')">开始跑</button>
          <button class="danger" onclick="act('stop')">停止</button>
          <button onclick="act('restart')">重启</button>
          <span id="msg3"></span>
        </div>
        <div class="hint" style="margin-top:8px">
          「开始跑」= 用这个项目自己的工作区(<code id="projdir">-</code>)去拉题、解题,不会覆盖或影响其它项目。
          模型、token、比赛接口在「控制台」里统一配置,所有项目共用。
        </div>
      </div>
      <div class="kpis">
        <div class="kpi"><div class="k">已解出</div><div class="v num" id="kpi_solved">-</div>
          <div class="s" id="kpi_solved_s">-</div></div>
        <div class="kpi"><div class="k">运行中 agent</div><div class="v num" id="kpi_workers">-</div>
          <div class="s" id="kpi_workers_s">-</div></div>
        <div class="kpi"><div class="k">排队 / 放弃</div><div class="v num" id="kpi_queue">-</div>
          <div class="s" id="kpi_queue_s">-</div></div>
        <div class="kpi"><div class="k">提交成功率</div><div class="v num" id="kpi_sub">-</div>
          <div class="s" id="kpi_sub_s">-</div></div>
      </div>
      <div class="toolbar">
        <input type="search" id="q" placeholder="搜索题目 / 大类 / ID   (Ctrl+K)" oninput="renderList()">
        <span class="chip active" data-f="all" onclick="setFilter('all')">全部 <b id="c_all">0</b></span>
        <span class="chip" data-f="running" onclick="setFilter('running')">运行中 <b id="c_running">0</b></span>
        <span class="chip" data-f="pending" onclick="setFilter('pending')">待派发 <b id="c_pending">0</b></span>
        <span class="chip" data-f="solved" onclick="setFilter('solved')">已解出 <b id="c_solved">0</b></span>
        <span class="chip" data-f="failed" onclick="setFilter('failed')">已放弃/中断 <b id="c_failed">0</b></span>
      </div>
      <div class="split">
        <div class="list" id="list"><div class="empty">等待数据…</div></div>
        <div class="detail" id="detail"><div class="empty">从左侧选择一道题查看实时进展</div></div>
      </div>
    </div>

    <!-- 控制台 -->
    <div class="view" id="v-control">
      <div class="card" style="margin-bottom:14px">
        <h2>比赛接口 <span class="pill" id="contest_pill">默认地址</span></h2>
        <div class="form">
          <div class="f"><label>队伍 token</label>
            <input type="text" id="f_TEAM_TOKEN" oninput="dirty=true"></div>
          <div class="f"><label>接口域名 Base</label>
            <input type="text" id="f_CONTEST_BASE" oninput="dirty=true"></div>
          <div class="f"><label>查题 路径(或整条 URL)</label>
            <input type="text" id="f_CONTEST_QUERY_PATH" oninput="dirty=true"></div>
          <div class="f"><label>重置环境 路径(或整条 URL)</label>
            <input type="text" id="f_CONTEST_RESET_PATH" oninput="dirty=true"></div>
          <div class="f"><label>提交 flag 路径(或整条 URL)</label>
            <input type="text" id="f_CONTEST_SUBMIT_PATH" oninput="dirty=true"></div>
        </div>
        <div class="btns">
          <button onclick="act('test-contest')">测试接口</button>
          <span id="msg2"></span>
        </div>
        <div class="hint" style="margin-top:8px">
          路径填法:只想换域名就改 <b>Base</b>;某条接口路径变了就改对应那一行;
          如果主办方直接给了完整 URL,粘进去即可(以 http 开头时原样使用,不再拼 Base)。
          改完点 <b>测试接口</b>,能立刻看到"拉到几道题"或具体报错。
        </div>
      </div>
      <div class="card" style="margin-bottom:14px">
        <h2>模型配置 <span class="pill" id="prov_pill">provider wqh</span></h2>
        <div class="form">
          <div class="f"><label>Base URL</label>
            <input type="text" id="f_PI_BASE_URL" oninput="dirty=true"></div>
          <div class="f"><label>API Key</label>
            <input type="text" id="f_PI_API_KEY" oninput="dirty=true"></div>
          <div class="f"><label>模型名</label>
            <input type="text" id="f_PI_MODEL" oninput="dirty=true"></div>
          <div class="f"><label>API 类型</label>
            <select id="f_PI_API_TYPE" onchange="dirty=true">
              <option value="openai-completions">openai-completions · Chat Completions(最通用)</option>
              <option value="anthropic-messages">anthropic-messages · Anthropic Messages</option>
              <option value="openai-responses">openai-responses · Responses API</option>
              <option value="google-generative-ai">google-generative-ai</option>
            </select></div>
        </div>
      </div>
      <div class="card">
        <h2>运行参数</h2>
        <div class="form">
          <div class="f"><label>起始并发(抢跑)</label>
            <input type="text" id="f_START_WORKERS" oninput="dirty=true"></div>
          <div class="f"><label>模式</label>
            <select id="f_IGNORE_SOLVED" onchange="dirty=true">
              <option value="0">0 · 正式比赛</option><option value="1">1 · 练习(重跑已解出的题)</option>
            </select></div>
        </div>
        <div class="btns">
          <button class="primary" onclick="save()">保存配置</button>
          <button onclick="act('test')">测试模型</button>
          <button class="primary" onclick="act('start')">启动 runner</button>
          <button class="danger" onclick="act('stop')">停止</button>
          <button onclick="act('restart')">重启</button>
          <span id="msg"></span>
        </div>
        <div class="sect"><div class="lbl">说明</div>
          <div class="hint">
            保存后会写入 <b>.env</b> 并自动同步成 pi 的 provider(<b>wqh</b>),命令行不用再动。<br>
            <b>测试模型</b> 会用 pi 实跑一次确认连通;<b>停止</b> 会优雅收掉所有 agent(不留孤儿进程)。<br>
            模型/token 改动需点 <b>重启</b> 才生效。没有代理的环境直接留空代理相关设置即可,比赛接口始终直连。
          </div>
        </div>
      </div>
    </div>

    <!-- 记录 -->
    <div class="view" id="v-records">
      <div class="card" style="margin-bottom:14px">
        <h2>提交记录</h2>
        <div id="subs"><div class="empty">暂无</div></div>
      </div>
      <div class="card">
        <h2>事件流</h2>
        <div id="facts"><div class="empty">暂无</div></div>
      </div>
    </div>
  </main>
</div>
<script>
const esc = s => (s??'').toString().replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const FIELDS = ['TEAM_TOKEN','CONTEST_BASE','CONTEST_QUERY_PATH','CONTEST_RESET_PATH','CONTEST_SUBMIT_PATH',
                'PI_BASE_URL','PI_API_KEY','PI_MODEL','PI_API_TYPE','START_WORKERS','IGNORE_SOLVED'];
const TITLES = {overview:'总览', control:'控制台', records:'记录'};
let dirty = false, view = 'overview', filter = 'all', sel = null, state = null;
let curProject = localStorage.getItem('wqh_project') || '默认项目';

function applyTheme(light){
  document.body.classList.toggle('light', light);
  const b = document.getElementById('themebtn');
  if (b) b.textContent = light ? '🌙 深色' : '☀️ 浅色';
  localStorage.setItem('wqh_theme', light ? 'light' : 'dark');
}
function toggleTheme(){ applyTheme(!document.body.classList.contains('light')); }
applyTheme(localStorage.getItem('wqh_theme') === 'light');

function go(v){
  view = v; sel = sel;
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === v));
  document.querySelectorAll('.view').forEach(s => s.classList.toggle('active', s.id === 'v-' + v));
  document.getElementById('viewtitle').textContent = TITLES[v];
  render();
}
function setFilter(f){
  filter = f;
  document.querySelectorAll('.chip').forEach(c => c.classList.toggle('active', c.dataset.f === f));
  renderList();
}
function fill(cfg){
  if (dirty) return;
  for (const k of FIELDS){ const el = document.getElementById('f_'+k); if (el) el.value = cfg[k] ?? ''; }
}
function payload(){ const o = {}; for (const k of FIELDS) o[k] = document.getElementById('f_'+k).value; return o; }
async function post(path, body){
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body||{})});
  return await r.json();
}
function say(t, err){ const m = document.getElementById('msg'); m.textContent = t; m.className = err ? 'err' : ''; }
async function save(){ say('保存中…'); const r = await post('/api/config', payload());
  dirty = false; say(r.message || 'ok', !r.ok); tick(); }
async function act(a){
  const m3 = document.getElementById('msg3') || document.getElementById('msg');
  m3.textContent = a + ' 中…'; m3.className = '';
  if (a === 'test'){ const r = await post('/api/test-model'); say(r.message, !r.ok); return; }
  if (a === 'test-contest'){ const m=document.getElementById('msg2'); m.textContent=a+' 中…';
    const r = await post('/api/test-contest'); m.textContent=r.message; m.className = r.ok?'':'err'; return; }
  const r = await post('/api/runner', {action:a, project: curProject});
  m3.textContent = r.message; m3.className = r.ok ? '' : 'err'; tick();
}
function switchProject(name){ curProject = name; localStorage.setItem('wqh_project', name); sel = null; tick(); }
async function createProject(){
  const el = document.getElementById('newproj');
  const name = (el.value || '').trim();
  if (!name){ const m=document.getElementById('msg3'); m.textContent='请先填项目名'; m.className='err'; return; }
  const r = await post('/api/projects', {action:'create', name});
  const m = document.getElementById('msg3'); m.textContent = r.message; m.className = r.ok ? '' : 'err';
  if (r.ok){ el.value = ''; switchProject(name); }
}
function renderProjects(list){
  const sel = document.getElementById('proj');
  const names = (list || []).map(p => p.name);
  if (!names.includes(curProject)) curProject = names[0] || '默认项目';
  sel.innerHTML = (list || []).map(p =>
    `<option value="${esc(p.name)}" ${p.name===curProject?'selected':''}>${esc(p.name)}` +
    `${p.questions ? ' · ' + p.questions + ' 题' : ''}${p.builtin ? ' · 内置' : ''}</option>`).join('');
  const pr = (list || []).find(p => p.name === curProject) || {};
  document.getElementById('proj_pill').textContent = curProject;
  document.getElementById('projdir').textContent = pr.dir || '-';
}
function matches(q){
  if (filter !== 'all'){
    if (filter === 'failed' ? !['failed','interrupted'].includes(q.key) : q.key !== filter) return false;
  }
  const kw = (document.getElementById('q').value || '').trim().toLowerCase();
  if (!kw) return true;
  return (q.title + ' ' + q.category + ' ' + q.qid + ' ' + (q.workdir||'')).toLowerCase().includes(kw);
}
function statusPill(q){
  const cls = {running:'run', solved:'ok', pending:'', failed:'err', interrupted:'warn'}[q.key] || '';
  const lab = {running:'运行中', solved:'已解出', pending:'待派发', failed:'已放弃', interrupted:'已中断'}[q.key] || q.key;
  return `<span class="pill ${cls}"><span class="dot"></span>${lab}</span>`;
}
function renderList(){
  if (!state) return;
  const qs = state.questions.filter(matches);
  const el = document.getElementById('list');
  if (!qs.length){ el.innerHTML = '<div class="empty">没有匹配的题目</div>'; return; }
  el.innerHTML = qs.map(q => `<div class="row ${sel===q.qid?'sel':''}" onclick="pick('${q.qid}')">
      <span>${statusPill(q)}</span>
      <span class="t"><span class="n">${esc(q.title)}</span>
        <span class="m">${esc(q.category)} · ${q.score}</span></span>
      <span class="m num" style="color:var(--text-faint);font-family:var(--mono);font-size:11px">
        ${q.workers.length ? q.workers.length+'×' : ''}${q.elapsed!=null? q.elapsed+'s' : (q.attempts? q.attempts+'次':'')}</span>
    </div>`).join('');
}
function pick(qid){ sel = qid; renderList(); renderDetail(); }
function renderDetail(){
  const q = state && state.questions.find(x => x.qid === sel);
  const el = document.getElementById('detail');
  if (!q){ el.innerHTML = '<div class="empty">从左侧选择一道题查看实时进展</div>'; return; }
  const conn = Object.entries(q.connection||{}).map(([k,v]) => `<code>${esc(v)}</code>`).join(' ');
  const ws = q.workers.length ? q.workers.map(w => `<div class="wk">
      <div class="wh"><span class="pill run"><span class="dot"></span>w${w.slot}</span>
        <span>运行 ${w.age}s</span><span style="color:var(--text-faint)">静默 ${w.quiet}s</span>
        <span style="color:var(--text-faint)">tokens ${w.tokens||'-'}</span>
        ${w.quiet>180?'<span class="pill err">可能卡住</span>':''}</div>
      ${(w.actions||[]).map(a=>`<div class="act">${esc(a)}</div>`).join('')}
      ${w.speech?`<div class="act say">💬 ${esc(w.speech)}</div>`:''}
      ${(w.errors||[]).map(e=>`<div class="act err">⚠ ${esc(e)}</div>`).join('')}
    </div>`).join('') : '<div class="meta">当前没有 agent 在跑</div>';
  el.innerHTML = `
    <div class="hd"><h3>${esc(q.title)}</h3>${statusPill(q)}</div>
    <div class="meta">${esc(q.category)} · ${q.score} 分 · 尝试 ${q.attempts}/${q.max_attempts}
      ${q.elapsed!=null?` · 已跑 <span class="num">${q.elapsed}s</span>`:''}</div>
    ${q.description?`<div class="meta">${esc(q.description)}</div>`:''}
    ${q.workdir?`<div class="meta">工作区 <code>work/${esc(q.workdir)}</code></div>`:''}
    ${conn?`<div class="meta">容器 ${conn}</div>`:''}
    <div class="sect"><div class="lbl">agent 实时动作</div>${ws}</div>
    ${q.shared?`<div class="sect"><div class="lbl">共享黑板 SHARED.md</div><pre>${esc(q.shared)}</pre></div>`:''}`;
}
function renderKpis(){
  const qs = state.questions;
  const by = k => qs.filter(q => q.key === k).length;
  document.getElementById('kpi_solved').textContent = by('solved') + '/' + qs.length;
  document.getElementById('kpi_solved_s').textContent = qs.length ? Math.round(by('solved')/qs.length*100) + '% 完成' : '-';
  document.getElementById('kpi_workers').textContent = state.running_workers;
  document.getElementById('kpi_workers_s').textContent = '并发上限 ' + (state.limit ?? '-');
  document.getElementById('kpi_queue').textContent = by('pending') + ' / ' + (by('failed') + by('interrupted'));
  document.getElementById('kpi_queue_s').textContent = '待派发 / 已放弃·中断';
  const subs = state.submissions || [];
  const okSub = subs.filter(s => s.ok).length;
  document.getElementById('kpi_sub').textContent = subs.length ? Math.round(okSub/subs.length*100) + '%' : '-';
  document.getElementById('kpi_sub_s').textContent = `最近 ${subs.length} 次提交`;
  for (const k of ['all','running','pending','solved','failed']){
    const n = k === 'all' ? qs.length
      : k === 'failed' ? by('failed') + by('interrupted') : by(k);
    const el = document.getElementById('c_' + k); if (el) el.textContent = n;
  }
}
function renderRecords(){
  const subs = state.submissions || [];
  document.getElementById('subs').innerHTML = subs.length
    ? `<table><thead><tr><th style="width:150px">时间</th><th style="width:170px">题目</th>
        <th style="width:70px">结果</th><th>flag</th></tr></thead><tbody>` +
      subs.map(r => `<tr><td>${esc(r.ts)}</td><td class="q">${esc(r.question_id)}</td>
        <td><span class="pill ${r.ok?'ok':'err'}"><span class="dot"></span>${r.ok?'正确':'失败'}</span></td>
        <td>${esc(r.flag)}</td></tr>`).join('') + '</tbody></table>'
    : '<div class="empty">暂无提交</div>';
  const facts = state.facts || [];
  document.getElementById('facts').innerHTML = facts.length
    ? facts.map(f => `<div class="ev"><span class="ts">${esc(f.ts)}</span>
        <span class="src">${esc(f.source)}</span><span class="tx">${esc(f.text)}</span></div>`).join('')
    : '<div class="empty">暂无事件</div>';
}
function render(){ renderKpis(); renderList(); renderDetail(); renderRecords(); }
async function tick(){
  try{
    state = await (await fetch('/api/state?project=' + encodeURIComponent(curProject))).json();
    state.questions.forEach(q => { q.key = statusKey(q); });
    fill(state.config || {});
    if (state.project) curProject = state.project;
    renderProjects(state.projects);
    const run = state.runner && state.runner.running;
    document.getElementById('k_run').textContent = run ? ('运行中 #' + state.runner.pid) : '未运行';
    document.getElementById('k_proj').textContent = curProject;
    document.getElementById('k_prog').textContent = state.solved + '/' + state.total;
    document.getElementById('k_work').textContent = state.running_workers;
    document.getElementById('k_pause').textContent = state.paused ? '⚠ 模型额度暂停中' : '';
    document.getElementById('sfoot').textContent = state.now + ' · ' + (state.limit ?? '-') + ' 并发';
    if (!sel && state.questions.length) sel = state.questions[0].qid;
    render();
  }catch(e){ document.getElementById('sfoot').textContent = '连接失败'; }
}
function statusKey(q){
  if (q.status_label === '已中断') return 'interrupted';
  return q.status;
}
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k'){
    e.preventDefault(); go('overview'); document.getElementById('q').focus();
  }
});
tick(); setInterval(tick, 2000);
</script></body></html>
"""
class Handler(BaseHTTPRequestHandler):
    def _send(self, obj_or_bytes, ctype: str) -> None:
        body = (obj_or_bytes if isinstance(obj_or_bytes, bytes)
                else json.dumps(obj_or_bytes, ensure_ascii=False).encode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/state":
            q = parse_qs(urlparse(self.path).query)
            self._send(build_state((q.get("project") or [DEFAULT_PROJECT])[0]),
                       "application/json; charset=utf-8")
        elif path in ("/", "/index.html"):
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
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
        elif path == "/api/test-model":
            msg = test_model()
            self._send({"ok": msg.startswith("模型连通正常"), "message": msg},
                       "application/json; charset=utf-8")
        elif path == "/api/runner":
            action = (payload.get("action") or "").lower()
            project = payload.get("project") or DEFAULT_PROJECT
            if action == "start":
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
                msg = create_project(payload.get("name") or "")
                self._send({"ok": msg.startswith("已创建"), "message": msg,
                            "projects": list_projects()}, "application/json; charset=utf-8")
            else:
                self._send({"ok": True, "projects": list_projects()}, "application/json; charset=utf-8")
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
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
