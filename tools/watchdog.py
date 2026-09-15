#!/usr/bin/env python3
"""比赛期间看护进程:定期体检 + 自动补救,全部写进 <项目>/logs/watchdog.log。

  python3 tools/watchdog.py --project wqb2            # 常驻看护(推荐:后台跑)
  python3 tools/watchdog.py --project wqb2 --once     # 只体检一次并打印(人工排查用)
  python3 tools/watchdog.py --project wqb2 --no-heal  # 只看不救(复盘用)

看护内容:
  1) 控制台(8799)还活着吗 —— 挂了就带"直连"环境重新 nohup 拉起(否则网页看不到进度、也没法补救)。
  2) runner 还活着吗 —— 窗口内掉了就调控制台接口重新拉起;心跳停滞(>180s)判定假死,杀掉重拉。
  3) 扫 runner.out 尾部,统计额度/限流/网络类错误(只记录:降并发与退避由 harness 自己做)。
  4) 窗口结束(deadline 过期)后停止一切补救并自行退出,不做无谓的重复拉起。

"还没点开始跑"与"跑挂了"的区分:只有当 deadline 文件存在且未过期时才视为"本该在跑",因此
比赛开始前它不会自己拉起 runner —— 一定要在网页上点过「开始跑」,看护才会接管。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))          # 复用 contest_api / config(与 harness 同一份配置)
DEFAULT_PROJECT = "默认项目"
DASHBOARD_PORT = 8799
PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

# 看护阈值
HEARTBEAT_WARN = 60      # 心跳超过这么久:告警
HEARTBEAT_DEAD = 180     # 心跳超过这么久:判假死,杀掉重拉
RESTART_COOLDOWN = 30    # 两次补救之间的最小间隔(秒),避免重启风暴
PLATFORM_INTERVAL = 45   # 开赛前探测"是否提前放题"的间隔(秒),别把查题接口打太频繁


def log(msg: str, path: Path) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def project_dir(name: str) -> Path:
    """与 dashboard.project_dir 一致:默认项目=代码根目录,其它在 projects/<slug>/。"""
    if name == DEFAULT_PROJECT:
        return ROOT
    slug = re.sub(r"[^A-Za-z0-9_.\-]+", "-", name.strip()).strip("-") or "project"
    return ROOT / "projects" / slug


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _api(path: str, payload: dict | None = None, timeout: int = 8) -> dict | None:
    """调控制台接口;控制台不在就返回 None(由调用方决定是否重启)。"""
    url = f"http://127.0.0.1:{DASHBOARD_PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with _no_proxy_opener().open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def clean_env() -> dict:
    """直连环境:本机 launchd 里的全局代理可能已失效,交给它只会 Connection error。"""
    env = {**os.environ}
    for k in PROXY_KEYS:
        env.pop(k, None)
    return env


def dashboard_alive() -> bool:
    return _api("/api/state", timeout=5) is not None


def start_dashboard(path: Path) -> bool:
    logs = ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "dashboard.out", "a", encoding="utf-8") as out:
        subprocess.Popen([sys.executable, str(ROOT / "dashboard.py")], cwd=ROOT, env=clean_env(),
                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(25):
        time.sleep(0.4)
        if dashboard_alive():
            return True
    return False


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def runner_pid(rundir: Path) -> int | None:
    p = rundir / "logs" / "runner.pid"
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except ValueError:
        return None


def kill_runner_tree(rundir: Path, path: Path) -> None:
    pid = runner_pid(rundir)
    if not pid or not pid_alive(pid):
        return
    try:  # runner 自己带了优雅退出:先 SIGTERM,给它收 worker 的机会
        os.killpg(pid, signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            return
    for _ in range(20):
        time.sleep(0.25)
        if not pid_alive(pid):
            return
    log(f"runner(pid {pid}) SIGTERM 后仍未退出,升级 SIGKILL", path)
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        pass


def pi_count() -> int:
    try:
        r = subprocess.run(["pgrep", "-c", "^pi$"], capture_output=True, text=True, timeout=5)
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return -1


ERR_PATTERNS = {
    "额度/余额": re.compile(r"quota|insufficient|余额|额度|402", re.I),
    "限流 429": re.compile(r"429|too many requests|rate.?limit", re.I),
    "模型网络错": re.compile(r"Connection error|ECONNREFUSED|ENOTFOUND|fetch failed|ETIMEDOUT", re.I),
    "平台接口异常": re.compile(r"查题接口异常|提交异常|urlopen error", re.I),
}


def scan_errors(rundir: Path, n_bytes: int = 200_000) -> dict:
    f = rundir / "logs" / "runner.out"
    if not f.exists():
        return {}
    try:
        size = f.stat().st_size
        with open(f, "rb") as fh:
            fh.seek(max(0, size - n_bytes))
            text = fh.read().decode("utf-8", "ignore")
    except OSError:
        return {}
    return {name: len(rx.findall(text)) for name, rx in ERR_PATTERNS.items() if rx.search(text)}


def platform_questions() -> tuple[int, str]:
    """查平台当前有几道题(开赛前用:主办方可能提前放题)。失败返回 -1 与原因。"""
    try:
        import config as config_mod
        import contest_api
        token = (config_mod.read_env().get("TEAM_TOKEN") or "").strip()
        if not token:
            return -1, "未配置队伍 token"
        return len(contest_api.list_questions(token, timeout=12)), ""
    except Exception as e:
        return -1, f"{type(e).__name__}: {str(e)[:70]}"


def snapshot(rundir: Path) -> dict:
    st = read_json(rundir / "logs" / "runner_state.json")
    dl = read_json(rundir / "logs" / "run_deadline.json")
    pid = runner_pid(rundir)
    hb = st.get("heartbeat") or 0
    return {
        "pid": pid, "alive": bool(pid and pid_alive(pid)),
        "hb_age": (time.time() - hb) if hb else None,
        "deadline": dl.get("deadline") or 0,
        "left": (dl.get("deadline") - time.time()) if dl.get("deadline") else None,
        "limit": st.get("limit"), "running": st.get("running"),
        "paused": st.get("paused"), "logs_mb": st.get("logs_mb"),
        "pi": pi_count(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="比赛期间看护:体检 + 自动补救")
    ap.add_argument("--project", default=DEFAULT_PROJECT, help="项目名(默认项目=代码根目录)")
    ap.add_argument("--interval", type=int, default=20, help="体检间隔秒数(默认 20)")
    ap.add_argument("--once", action="store_true", help="只体检一次")
    ap.add_argument("--no-heal", action="store_true", help="只看不救")
    args = ap.parse_args()

    rundir = project_dir(args.project)
    wlog = rundir / "logs" / "watchdog.log"
    heal = not args.no_heal
    last_heal = 0.0
    last_plat = 0.0

    log(f"看护启动:项目「{args.project}」→ {rundir}(间隔 {args.interval}s,"
        f"{'只看不救' if not heal else '自动补救'})", wlog)
    if not rundir.exists():
        log(f"警告:项目目录不存在 {rundir}(项目名可能写错了)", wlog)

    while True:
        s = snapshot(rundir)
        dl, hb = s["deadline"], s["hb_age"]

        # ---- 窗口结束:收工
        if dl and s["left"] is not None and s["left"] <= 0:
            log(f"挑战窗口已结束(超时 {-s['left']:.0f}s),停止看护"
                + (f";仍有 {s['pi']} 个 pi 进程" if s["pi"] else ""), wlog)
            return 0

        bits = [f"runner={'alive pid ' + str(s['pid']) if s['alive'] else 'DOWN'}",
                f"pi={s['pi']}", f"limit={s['limit']}", f"running={s['running']}"]
        if s["left"] is not None:
            bits.append(f"剩余 {int(s['left'] // 60)}分{int(s['left'] % 60)}秒")
        if hb is not None:
            bits.append(f"心跳 {hb:.0f}s 前")
        if s["paused"]:
            bits.append("已暂停派发(额度)")
        errs = scan_errors(rundir)
        if errs:
            bits.append("错误: " + ", ".join(f"{k}×{v}" for k, v in errs.items()))
        log("状态 " + " | ".join(bits), wlog)

        now = time.time()

        # ---- 开赛前:盯平台有没有提前放题(放题了就提醒可以开跑)
        if dl == 0 and now - last_plat >= PLATFORM_INTERVAL:
            last_plat = now
            n, why = platform_questions()
            if n > 0:
                log(f"★ 平台已放出 {n} 道题 —— 可以开跑了", wlog)
            elif n == 0:
                log("平台已连通但题目列表为空(未放题)", wlog)
            else:
                log(f"平台暂未放题({why})", wlog)

        if heal and not args.once:
            if now - last_heal > RESTART_COOLDOWN:
                # ---- 控制台掉了:先救控制台(没有它就没法用网页补救/看进度)
                if not dashboard_alive():
                    log("控制台(8799)无响应 → 重新拉起", wlog)
                    ok = start_dashboard(wlog)
                    log("控制台已恢复" if ok else "控制台拉起失败,下轮再试", wlog)
                    last_heal = now
                # ---- 窗口内 runner 不在 = 本该在跑却掉了 → 接管重拉
                elif dl and not s["alive"]:
                    r = _api("/api/runner", {"action": "start", "project": args.project}, timeout=90)
                    log(f"窗口内 runner 掉了 → 重新拉起: {r.get('message') if r else '接口无响应'}", wlog)
                    last_heal = now
                # ---- 进程在但心跳停滞 = 假死 → 杀掉重拉
                elif s["alive"] and hb is not None and hb > HEARTBEAT_DEAD:
                    log(f"runner 心跳停滞 {hb:.0f}s(>{HEARTBEAT_DEAD}s),判定假死 → 杀掉重拉", wlog)
                    kill_runner_tree(rundir, wlog)
                    r = _api("/api/runner", {"action": "start", "project": args.project}, timeout=90)
                    log(f"重拉结果: {r.get('message') if r else '接口无响应'}", wlog)
                    last_heal = now
                elif s["alive"] and hb is not None and hb > HEARTBEAT_WARN:
                    log(f"注意:心跳已 {hb:.0f}s 未更新(>{HEARTBEAT_WARN}s),继续观察", wlog)

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n看护已停止(手动中断)")
