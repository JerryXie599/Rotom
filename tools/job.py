#!/usr/bin/env python3
"""题目级后台作业管理 —— 长扫描/爆破放后台跑,agent 不必阻塞等待。

用法(在 agent 的题目工作目录里,由 bash 工具调用):
  python3 /app/tools/job.py start "<命令>" [--name 短名]
  python3 /app/tools/job.py status <name|job_id>   [--tail 40]
  python3 /app/tools/job.py list
  python3 /app/tools/job.py stop <name|job_id>

设计要点(2026-10-26:对齐头部选手 job_start/status/list/stop 的做法):
  - **题目隔离**:registry 与日志都放在「当前题目工作区的 .jobs/」下;cwd 必须是一个以 w<数字> 结尾
    且带 .qid 的题目工作目录,越界(绝对路径/../ 逃出)直接拒绝。
  - **不阻塞**:start 立刻返回 job 名(命令用新会话/独立进程组跑,stdout+stderr 落 .jobs/<name>.log);
    到点查 status 即可,不占 worker 会话时间。
  - **安全**:registry 里记 pid/pgid/启动时间/cwd;stop 前核对 pgid + cwd + 进程启动时间,
    防止 pid 复用误杀;只允许操作本工作区的 job。
  - 进程组随 worker 一起死也没关系:registry 状态会标成 dead,日志还在。

退出码:0 成功;2 用法/越界;3 找不到 job;4 执行失败。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

JOB_DIRNAME = ".jobs"
MAX_LOG_BYTES = 8 * 1024 * 1024      # 单 job 日志上限(超出截断,防止把盘写满)


def _job_dir() -> Path:
    cwd = Path.cwd().resolve()
    if not re.search(r"[\\/]w\d+$", str(cwd)):
        print("job.py 必须在题目工作目录(…/w<数字>)里运行;当前: %s" % cwd, file=sys.stderr)
        sys.exit(2)
    if not (cwd / ".qid").exists() and not (cwd.parent / ".qid").exists():
        print("当前目录看起来不是题目工作区(缺 .qid);拒绝在其它位置起后台作业", file=sys.stderr)
        sys.exit(2)
    d = cwd / JOB_DIRNAME
    d.mkdir(exist_ok=True)
    return d


def _registry_path(d: Path) -> Path:
    return d / "registry.json"


def _load(d: Path) -> dict:
    p = _registry_path(d)
    if not p.exists():
        return {"jobs": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"jobs": {}}


def _save(d: Path, reg: dict) -> None:
    tmp = _registry_path(d).with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, _registry_path(d))


def _proc_started(pid: int) -> float:
    """内核里的进程启动时间(epoch);拿不到返回 0。用于 stop 时防 pid 复用。"""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        if not out:
            return 0.0
        return time.mktime(time.strptime(out))
    except Exception:
        return 0.0


def _alive(pid: int, started: float | None = None) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if started:
        cur = _proc_started(pid)
        if cur and abs(cur - started) > 3:      # 启动时间对不上 → pid 已被复用
            return False
    return True


def _find(reg: dict, key: str) -> tuple[str, dict] | tuple[None, None]:
    jobs = reg.get("jobs") or {}
    if key in jobs:
        return key, jobs[key]
    matches = [k for k, v in jobs.items() if v.get("name") == key or k == key]
    if len(matches) == 1:
        return matches[0], jobs[matches[0]]
    return None, None


def cmd_start(args: list[str]) -> int:
    name = ""
    if "--name" in args:
        i = args.index("--name")
        if i + 1 < len(args):
            name = args[i + 1].strip()
            del args[i:i + 2]
    if not args:
        print("用法: job.py start \"<命令>\" [--name 短名]", file=sys.stderr)
        return 2
    cmd = " ".join(args).strip()
    d = _job_dir()
    reg = _load(d)
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", name or f"job{len(reg['jobs']) + 1}")
    if name in reg["jobs"]:
        name = f"{name}.{int(time.time()) % 10000}"
    log = d / f"{name}.log"
    with open(log, "wb") as lf:
        proc = subprocess.Popen(["/bin/bash", "-lc", cmd], stdout=lf, stderr=subprocess.STDOUT,
                                start_new_session=True, cwd=str(Path.cwd()))
    reg["jobs"][name] = {
        "name": name, "cmd": cmd, "pid": proc.pid, "pgid": proc.pid,
        "started": time.time(), "proc_started": _proc_started(proc.pid),
        "cwd": str(Path.cwd().resolve()), "log": str(log), "state": "running",
    }
    _save(d, reg)
    print(f"已启动后台作业 [{name}] pid={proc.pid}\n  命令: {cmd}\n  日志: {log}")
    print(f"  查看: python3 tools/job.py status {name} --tail 40")
    return 0


def _refresh(entry: dict) -> dict:
    if entry.get("state") == "running" and not _alive(int(entry.get("pid") or 0),
                                                     entry.get("proc_started")):
        entry["state"] = "dead"
    return entry


def cmd_status(args: list[str]) -> int:
    tail = 40
    if "--tail" in args:
        i = args.index("--tail")
        try:
            tail = int(args[i + 1])
            del args[i:i + 2]
        except Exception:
            del args[i:i + 1]
    key = args[0] if args else ""
    d = _job_dir()
    reg = _load(d)
    if not key:
        return cmd_list([])
    k, e = _find(reg, key)
    if not e:
        print(f"没有找到作业「{key}」;先 list 看看。", file=sys.stderr)
        return 3
    _refresh(e)
    _save(d, reg)
    print(f"[{k}] state={e['state']} pid={e.get('pid')} 运行 {int(time.time() - float(e.get('started') or 0))}s")
    print(f"  命令: {e.get('cmd')}")
    print(f"  日志: {e.get('log')}")
    log = Path(str(e.get("log") or ""))
    if log.exists() and tail > 0:
        size = log.stat().st_size
        print(f"  --- 日志尾部({size} bytes)---")
        with open(log, "rb") as f:
            f.seek(max(0, size - 200_000))
            text = f.read().decode("utf-8", "ignore")
        for ln in text.splitlines()[-tail:]:
            print("  " + ln[:400])
    return 0


def cmd_list(args: list[str]) -> int:
    d = _job_dir()
    reg = _load(d)
    jobs = reg.get("jobs") or {}
    if not jobs:
        print("(还没有后台作业。启动: python3 tools/job.py start \"<命令>\")")
        return 0
    changed = False
    for e in jobs.values():
        before = e.get("state")
        _refresh(e)
        changed = changed or before != e.get("state")
    if changed:
        _save(d, reg)
    for k, e in sorted(jobs.items()):
        age = int(time.time() - float(e.get("started") or 0))
        print(f"  {k:<24} {e.get('state'):<8} {age:>6}s  {e.get('cmd', '')[:80]}")
    return 0


def cmd_stop(args: list[str]) -> int:
    key = args[0] if args else ""
    if not key:
        print("用法: job.py stop <name|job_id>", file=sys.stderr)
        return 2
    d = _job_dir()
    reg = _load(d)
    k, e = _find(reg, key)
    if not e:
        print(f"没有找到作业「{key}」", file=sys.stderr)
        return 3
    pid, pgid = int(e.get("pid") or 0), int(e.get("pgid") or 0)
    if not _alive(pid, e.get("proc_started")):
        e["state"] = "dead"
        _save(d, reg)
        print(f"[{k}] 早已退出")
        return 0
    # 双重校验:进程组 + 工作目录,防止 pid 复用误杀别的题
    try:
        cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
    except Exception:
        cwd = None
    if cwd is not None and str(cwd) != str(e.get("cwd")):
        print(f"[{k}] 校验失败:进程 cwd={cwd} 与登记不符,拒绝 kill", file=sys.stderr)
        return 4
    try:
        os.killpg(pgid, signal.SIGTERM)
        time.sleep(0.4)
        if _alive(pid, e.get("proc_started")):
            os.killpg(pgid, signal.SIGKILL)
        e["state"] = "stopped"
        e["stopped_at"] = time.time()
        _save(d, reg)
        print(f"[{k}] 已停止(进程组 {pgid})")
        return 0
    except Exception as ex:                                  # noqa: BLE001
        print(f"[{k}] 停止失败: {type(ex).__name__}: {ex}", file=sys.stderr)
        return 4


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "start":
        return cmd_start(args)
    if cmd == "status":
        return cmd_status(args)
    if cmd == "list":
        return cmd_list(args)
    if cmd == "stop":
        return cmd_stop(args)
    print(f"未知子命令 {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())