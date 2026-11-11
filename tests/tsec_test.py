#!/usr/bin/env python3
"""众测(zhongce)模式离线断言:TSec 适配器 + 多 flag 记账 + 容器名额调度 + 提交工具。

全部跑在 tests/mock_tsec_server.py 上,不需要真实平台/VPN/模型。

用法:
    python3 tests/tsec_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PLATFORM_PORT = 18830
VPN_PORT = 18831
TARGET_BASE = 18940          # mock_tsec_server 里的 TARGET_BASE_PORT 是 18900,这里用独立段避免撞车

PASS = FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32m✔\033[0m {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  \033[31m✘\033[0m {name} {extra}")


def section(t: str) -> None:
    print(f"\n== {t} ==")


def start_mock() -> None:
    import mock_tsec_server as mock
    mock.TARGET_BASE_PORT = TARGET_BASE
    mock._init_challenges(6)
    from http.server import ThreadingHTTPServer
    servers = [ThreadingHTTPServer(("127.0.0.1", PLATFORM_PORT), mock.PlatformHandler),
               ThreadingHTTPServer(("127.0.0.1", VPN_PORT), mock.VpnHandler)]
    for code, st in mock.STATE.items():
        h = type(f"T_{code}", (mock.TargetHandler,), {"challenge": code})
        servers.append(ThreadingHTTPServer(("127.0.0.1", st["port"]), h))
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()


def sync_until(token, entries, want, max_active=3, timeout=20.0):
    """反复调 sync_containers 直到 want 个容器就绪。

    真实平台的 start/close 是几十秒级慢接口,适配器改成异步(不阻塞调度循环),
    所以这里也要像 runner 那样"多轮调用 + 等平台状态"。
    """
    import tsec_api
    end = time.time() + timeout
    mapping = {}
    while time.time() < end:
        mapping = tsec_api.sync_containers(token, entries, max_active=max_active)
        if sum(1 for v in mapping.values() if v["container_ready"]) >= want:
            return mapping
        time.sleep(0.2)
    return mapping


def wait_status(token, qid, want, timeout=20.0):
    """等某题的容器状态变成 want(重新拉一次列表再看)。"""
    import tsec_api
    st = ""
    end = time.time() + timeout
    while time.time() < end:
        tsec_api.list_challenges(token)
        st = (tsec_api.cached_status().get(qid) or {}).get("container_status", "")
        if st == want:
            return st
        time.sleep(0.2)
    return st


def main() -> int:
    os.environ["CONTEST_ADAPTER"] = "tsec"
    os.environ["BENCHMARK_BASE_URL"] = f"http://127.0.0.1:{PLATFORM_PORT}"
    os.environ["BENCHMARK_TOKEN"] = "mock-benchmark-token"
    os.environ["VPN_CHECK_URL"] = f"http://127.0.0.1:{VPN_PORT}"
    tmp = tempfile.mkdtemp(prefix="rotom-tsec-test-")
    os.environ["ROTOM_RUN_DIR"] = tmp
    os.environ["MAX_ACTIVE_CHALLENGES"] = "3"
    os.environ["QUESTION_SLICE_SECONDS"] = "5"
    os.environ["ROTATE_COOLDOWN"] = "1"

    start_mock()
    time.sleep(0.3)

    import board as board_mod
    import tsec_api
    import runner

    TOK = "mock-benchmark-token"

    # ---------------------------------------------------------------- VPN
    section("VPN 预检")
    r = tsec_api.check_vpn()
    check("VPN 预检通过(status=ok)", r["ok"] is True, str(r))
    old = os.environ["VPN_CHECK_URL"]
    os.environ["VPN_CHECK_URL"] = "http://127.0.0.1:1"      # 黑洞端口 = 没连 VPN
    r2 = tsec_api.check_vpn(timeout=2)
    check("VPN 不通时 ok=False(不抛异常)", r2["ok"] is False and bool(r2["error"]))
    os.environ["VPN_CHECK_URL"] = old

    # ---------------------------------------------------------------- 题目列表
    section("题目列表 / 字段映射")
    qs = tsec_api.list_questions(TOK)
    check("拉到 6 道题", len(qs) == 6, str(len(qs)))
    by = {q["question_id"]: q for q in qs}
    q1 = by["web_sql_injection_01"]
    check("多 flag 题 flag_count=2", q1["flag_count"] == 2, str(q1["flag_count"]))
    check("未启动时 container_ready=False", q1["container_ready"] is False)
    check("未启动时没有目标地址", q1["connection"] == {})
    check("分类推断为 pentest", q1["category"] == "pentest", q1["category"])

    # ---------------------------------------------------------------- 容器调度
    section("容器调度(平台硬上限 3)")
    board = {"questions": {}}
    entries = [board_mod.upsert_question(board, q) for q in qs]
    t0 = time.time()
    tsec_api.sync_containers(TOK, entries, max_active=3)     # 单次调用必须立刻返回(慢接口走后台)
    dt = time.time() - t0
    check("sync_containers 单次调用不阻塞(<2s)", dt < 2.0, f"{dt:.2f}s")
    mapping = sync_until(TOK, entries, 3)
    ready = [k for k, v in mapping.items() if v["container_ready"]]
    check("最多同时拉起 3 个容器", len(ready) == 3, str(ready))
    check("容器地址已返回", all(mapping[k]["container_addr"] for k in ready))
    check("地址映射成 http url", mapping[ready[0]]["connection"].get("url1", "").startswith("http://"),
          str(mapping[ready[0]]["connection"]))
    raw = {c["unique_code"]: c for c in tsec_api.list_challenges(TOK)}
    check("平台侧确认 3 个 available",
          sum(1 for c in raw.values() if c["container_status"] == "available") == 3)

    # 第四道题应该顶掉一个(模拟"想跑的题变了"):先关后开,不能撞"活跃上限"
    first = entries[0]["question_id"]
    others = [e for e in entries if e["question_id"] != first]
    mapping2 = sync_until(TOK, others, 3, timeout=60.0)
    st_first = wait_status(TOK, first, "stopped")
    check("轮换后旧容器被关闭", st_first == "stopped", st_first)
    check("新的一组也是 3 个(先关后开没撞上限)",
          sum(1 for v in mapping2.values() if v["container_ready"]) == 3,
          str({k: v["container_status"] for k, v in mapping2.items()}))

    # 全部关掉,后面按需再开
    tsec_api.close_all(TOK)
    raw3 = list(tsec_api.list_challenges(TOK))
    check("close_all 关闭全部容器",
          sum(1 for c in raw3 if c["container_status"] == "available") == 0,
          str([(c["unique_code"], c["container_status"]) for c in raw3]))

    # ---------------------------------------------------------------- 防空转
    section("容器名额稳定性(真机踩过的坑:26 秒换一批容器)")
    board2 = {"questions": {}}
    ents = [board_mod.upsert_question(board2, q) for q in tsec_api.list_questions(TOK)]
    hold = sync_until(TOK, ents, 3)
    held = sorted(k for k, v in hold.items() if v["container_ready"])
    check("先占住 3 个名额", len(held) == 3, str(held))
    # 模拟 runner 派完 agent 后的状态:这 3 道题 attempts 从 0 变 1、状态 running。
    # 旧排序把 attempts 当第一优先级 → 它们会立刻被没碰过的题挤出名单 → 容器被关 → agent 打空。
    for e in ents:
        if e["question_id"] in held:
            e["attempts"] = 1
            e["status"] = board_mod.STATUS_RUNNING
    m2 = {}
    for _ in range(8):
        m2 = tsec_api.sync_containers(TOK, ents, max_active=3)
        time.sleep(0.3)
    held2 = sorted(k for k, v in m2.items() if v["container_ready"])
    check("拿到尝试机会的题仍保住名额(现任优先,不空转)", held2 == held, f"{held} -> {held2}")
    plat = {c["unique_code"]: c["container_status"] for c in tsec_api.list_challenges(TOK)}
    check("平台侧也还是同一批容器在跑",
          sorted(q for q, s in plat.items() if s == "available") == held,
          str({q: s for q, s in plat.items() if s != "stopped"}))
    # 只有 rotate() 能让现任让位
    for e in ents:
        if e["question_id"] in held:
            e["rotate_until"] = time.time() + 60      # 模拟时间片用满被轮换
    m3 = sync_until(TOK, ents, 3, timeout=40.0)
    held3 = sorted(k for k, v in m3.items() if v["container_ready"])
    check("rotate 标记过的题才会让位(换成新的一批)",
          len(held3) == 3 and not (set(held3) & set(held)), f"{held} -> {held3}")
    tsec_api.close_all(TOK)

    # ---------------------------------------------------------------- 提交
    section("提交 / 多 flag / 重复提交")
    qid = "web_sql_injection_01"
    tsec_api.start_challenge(TOK, qid)
    r1 = tsec_api.submit_flag(TOK, qid, "flag{sql_1}")
    check("第一个 flag 正确", tsec_api.is_correct(r1), str(r1))
    check("平台返回 1/2 进度", r1.get("correct_flag_count") == 1 and r1.get("total_flag_count") == 2, str(r1))
    check("matched_flag_index=0", r1.get("matched_flag_index") == 0)
    rdup = tsec_api.submit_flag(TOK, qid, "flag{sql_1}")
    check("重复提交被消解成「已正确」而不是错误",
          rdup.get("code") == 0 and rdup.get("duplicate") is True and tsec_api.is_correct(rdup), str(rdup))
    rwrong = tsec_api.submit_flag(TOK, qid, "flag{nope}")
    check("错误 flag:受理了但 correct=False", tsec_api.is_verdict(rwrong) and not tsec_api.is_correct(rwrong), str(rwrong))
    r2 = tsec_api.submit_flag(TOK, qid, "flag{sql_2}")
    check("第二个 flag 正确,进度 2/2",
          r2.get("correct_flag_count") == 2 and r2.get("matched_flag_index") == 1, str(r2))
    done = [q for q in tsec_api.list_questions(TOK) if q["question_id"] == qid][0]
    check("平台标记 is_completed", done["is_solved"] is True)

    # ---------------------------------------------------------------- 提交工具
    section("tools/submit_flag.py(黑板记账 + 通关判定)")
    board2 = {"questions": {}}
    q_meta = [q for q in tsec_api.list_questions(TOK) if q["question_id"] == "web_auth_bypass_03"][0]
    board_mod.upsert_question(board2, q_meta)
    (Path(tmp) / "board.json").write_text(json.dumps(board2, ensure_ascii=False), encoding="utf-8")
    tsec_api.start_challenge(TOK, "web_auth_bypass_03")

    env = {**os.environ, "CONTEST_ADAPTER": "tsec", "ROTOM_RUN_DIR": tmp,
           "BENCHMARK_TOKEN": TOK, "TEAM_TOKEN": TOK}
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "submit_flag.py"),
                        "web_auth_bypass_03", "flag{authz_1}"],
                       capture_output=True, text=True, env=env)
    check("提交工具返回 CORRECT(退出码 0)", p.returncode == 0, p.stdout + p.stderr)
    check("未通关时提示还有 flag 没拿", "还有 1 个 flag" in p.stdout, p.stdout)
    after = json.loads((Path(tmp) / "board.json").read_text(encoding="utf-8"))
    e = after["questions"]["web_auth_bypass_03"]
    check("黑板记到 1/2", int(e["correct_flag_count"]) == 1 and int(e["flag_count"]) == 2, str(e))
    check("黑板未标 solved(还没通关)", e["status"] != board_mod.STATUS_SOLVED, e["status"])

    p2 = subprocess.run([sys.executable, str(ROOT / "tools" / "submit_flag.py"),
                         "web_auth_bypass_03", "flag{authz_2}"],
                        capture_output=True, text=True, env=env)
    check("第二个 flag 提交成功", p2.returncode == 0 and "本题通关" in p2.stdout, p2.stdout)
    after = json.loads((Path(tmp) / "board.json").read_text(encoding="utf-8"))
    check("黑板标 solved", after["questions"]["web_auth_bypass_03"]["status"] == board_mod.STATUS_SOLVED)

    pw = subprocess.run([sys.executable, str(ROOT / "tools" / "submit_flag.py"),
                         "web_auth_bypass_03", "flag{bad}"],
                        capture_output=True, text=True, env=env)
    check("错 flag 返回 WRONG(退出码 1)", pw.returncode == 1 and "WRONG" in pw.stdout, pw.stdout)

    section("容器名额轮换(runner.rotate)")
    tsec_api.close_all(TOK)
    board3 = {"questions": {}, "facts": [], "started_at": time.time()}
    now = time.time()
    running: list[str] = []
    victim = ""
    for q in tsec_api.list_questions(TOK):
        if q["question_id"] in ("web_sql_injection_01", "web_auth_bypass_03"):
            continue                      # 这两道已经通关了,不在候选里
        e = board_mod.upsert_question(board3, q)
        if len(running) < 3:              # 3 道题正占着容器名额,时间片已经用满
            e["attempts"] = 1
            e["status"] = board_mod.STATUS_RUNNING
            e["slice_start"] = now - 10
            if len(running) == 2:         # 第 3 道在这一片里刚拿到 flag → 应该续期而不是被赶走
                e["correct_flag_count"] = 1
                e["slice_flags"] = 0
                victim = q["question_id"]
            else:
                e["slice_flags"] = 0
            running.append(q["question_id"])
        # 剩下 1 道是"还没碰过"的题在排队 → 才有轮换的理由
    (Path(tmp) / "board.json").write_text(json.dumps(board3, ensure_ascii=False), encoding="utf-8")

    runner.QUESTION_SLICE_SECONDS = 5
    runner.ROTATE_COOLDOWN = 1
    runner.MAX_ACTIVE_CHALLENGES = 3
    runner.rotate({})
    b = json.loads((Path(tmp) / "board.json").read_text(encoding="utf-8"))
    rotated = [q for q, e in b["questions"].items() if float(e.get("rotate_until") or 0) > 0]
    check("时间片用满且无进展的题被轮换让位", len(rotated) >= 1, str(rotated))
    check("有新进展的题被续期,不被轮换",
          float(b["questions"][victim].get("rotate_until") or 0) == 0
          and float(b["questions"][victim].get("slice_start") or 0) > 0,
          str(b["questions"][victim].get("rotate_until")))
    check("不会把全部题目轮空(排队的题仍能上)",
          len([1 for e in b["questions"].values()
               if float(e.get("rotate_until") or 0) == 0]) >= 1)
    check("被轮换的题回到了 pending 并进入冷却",
          all(b["questions"][q]["status"] == board_mod.STATUS_PENDING
              and b["questions"][q]["next_retry_at"] > now for q in rotated))

    # ---------------------------------------------------------------- 错误处理
    section("错误处理")
    e1 = tsec_api.submit_flag("wrong-token", "web_idor_02", "flag{x}")
    check("token 无效 → task_not_found 且标记 task_over",
          e1.get("code") == "task_not_found" and e1.get("task_over") is True, str(e1))
    e2 = tsec_api.submit_flag(TOK, "no_such_challenge", "flag{x}")
    check("题目不存在 → challenge_not_found(不致命)", e2.get("code") == "challenge_not_found"
          and not e2.get("task_over"), str(e2))
    check("is_rate_limited 只认暂时性错误",
          tsec_api.is_rate_limited({"code": "resource_unavailable"})
          and not tsec_api.is_rate_limited({"code": "challenge_not_found"}))

    # ------------------------------------------------ 容器服务级探活 + 自愈
    section("容器服务级探活 + 自愈(平台报 available ≠ 容器里的服务活着)")
    import socket as _socket
    import http.server as _httpd

    check("_parse_addr 认 IP:端口 / URL / 裸 IP",
          tsec_api._parse_addr("10.0.1.2:8080") == ("10.0.1.2", 8080)
          and tsec_api._parse_addr("http://10.0.1.2:8080/x") == ("10.0.1.2", 8080)
          and tsec_api._parse_addr("10.0.1.2") == ("10.0.1.2", 80),
          str(tsec_api._parse_addr("10.0.1.2:8080")))

    # ① 关着的端口 → 判死
    s = _socket.socket(); s.bind(("127.0.0.1", 0)); dead_port = s.getsockname()[1]; s.close()
    ok_dead, why_dead = tsec_api._probe_alive(f"127.0.0.1:{dead_port}", 1.0, 1.0)
    check("端口关着 → 判死(原因带 TCP 不通)", ok_dead is False and "TCP" in why_dead, why_dead)

    # ② 活的 HTTP 服务 → 判活
    class _H(_httpd.BaseHTTPRequestHandler):
        def do_GET(self):                                   # noqa: N802
            self.send_response(200); self.send_header("Content-Length", "2")
            self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a):                          # 静音
            pass
    live = _httpd.HTTPServer(("127.0.0.1", 0), _H)
    live_port = live.server_address[1]
    threading.Thread(target=live.serve_forever, daemon=True).start()
    ok_live, why_live = tsec_api._probe_alive(f"127.0.0.1:{live_port}", 1.0, 2.0, "always")
    check("HTTP 有响应 → 判活", ok_live is True and "HTTP" in why_live, why_live)

    # ③ "假死":TCP 能连(内核 backlog)但应用层永远不回 —— 实测 c-02 就是这种状态
    hung = _socket.socket(); hung.bind(("127.0.0.1", 0)); hung.listen(4)
    hung_port = hung.getsockname()[1]
    ok_hung, why_hung = tsec_api._probe_alive(f"127.0.0.1:{hung_port}", 1.0, 1.0, "always")
    check("TCP 能连但 HTTP 不回 → 判死(只探 TCP 会漏掉这种)", ok_hung is False and "HTTP" in why_hung, why_hung)

    # ④ 连续判死到阈值 → 强制 close(下一轮自然拉新容器)
    for d in (tsec_api._HEALTH_FAILS, tsec_api._HEALTH_LAST, tsec_api._HEALTH_HOLD,
              tsec_api._HEALTH_HEALS, tsec_api._HEALTH_ADDR, tsec_api._HEALTH_FUT):
        d.clear()
    closed: list[str] = []
    _orig_close = tsec_api.close_async
    tsec_api.close_async = lambda tok, qid: closed.append(qid)
    try:
        health = {"enabled": True, "probe_interval_s": 0, "fail_threshold": 2,
                  "tcp_timeout_s": 1, "http_timeout_s": 1, "http_check": "always",
                  "max_heal_per_question": 1, "heal_cooldown_s": 0}
        status = {"q1": {"container_status": "available", "container_addr": [f"127.0.0.1:{dead_port}"]}}
        entries = [{"question_id": "q1", "is_solved": False}]
        notes: dict = {}
        # 别用固定圈数(12×0.15s 在忙的机器上不够两个 http_timeout 探完 → 假失败):
        # 改成"有结果或到点"的期限等待(实测压到 ~1.8s 会误判,放宽到 8s)
        deadline = time.time() + 8
        while time.time() < deadline and not closed:
            notes = tsec_api._health_tick(TOK, status, {"q1": entries[0]}, time.time(), health)
            time.sleep(0.15)
        check("连续 2 次探不通 → 发一次 close 换容器",
              closed == ["q1"], f"closed={closed} notes={notes}")
        check("说明里写明是探活判死(不是平台状态)", "强制换容器" in (notes.get("q1") or ""), str(notes))
        # ⑤ 换容器次数上限:再判死也不重启(避免死循环换容器)
        deadline = time.time() + 8
        while time.time() < deadline and "不再自动重启" not in (notes.get("q1") or ""):
            notes = tsec_api._health_tick(TOK, status, {"q1": entries[0]}, time.time(), health)
            time.sleep(0.15)
        check("换过 1 次后不再自动重启(上限生效,留给人工)",
              closed == ["q1"] and "不再自动重启" in (notes.get("q1") or ""), f"closed={closed} notes={notes}")
        # ⑥ 已解出的题不探活
        tsec_api._HEALTH_FAILS.clear(); closed.clear()
        solved_entry = dict(entries[0], is_solved=True)
        tsec_api._health_tick(TOK, status, {"q1": solved_entry}, time.time(), health)
        time.sleep(0.2)
        check("已解出的题不做探活(不打扰已完成的题)", not closed and not tsec_api._HEALTH_FUT)
    finally:
        tsec_api.close_async = _orig_close
        live.shutdown(); hung.close()
    # ---------------------------------------------------------------- harness 侧补交
    # 第六次 Docker 测试:c-03 的 flag 值核对过、通道坏掉交不上,agent 自己写重试循环烧掉几小时。
    # 现在由 harness 兜:平台没受理的提交记进 logs/pending_flags.jsonl,通道恢复后补交一次。
    section("已确证 flag 的补交(tools/backfill_flags.py)")
    import urllib.request  # noqa: E402

    sys.path.insert(0, str(ROOT / "tools"))        # backfill_flags 住在 tools/
    import backfill_flags  # noqa: E402

    def mock_busy(flags: list) -> dict:
        # **显式绕开本机代理**:macOS 的系统代理(127.0.0.1:7877)会拦本地 mock,而它的
        # ExceptionsList 里写的是 `127.*` —— Python 的代理旁路匹配认不出来,于是本地请求被转成 502
        # (踩过:同一份测试在代理关着时全绿、代理开着时挂)。老规矩:凡本地/靶场请求都显式 bypass。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            f"http://127.0.0.1:{PLATFORM_PORT}/__mock__/busy",
            data=json.dumps({"flags": flags}).encode(), method="POST")
        return json.loads(opener.open(req, timeout=5).read())

    # 测试里把重试压到 1 次 / 0 延迟(默认 5 次 + 退避,不然要等十几秒)
    tsec_api.SUBMIT_ATTEMPTS, tsec_api.SUBMIT_BASE_DELAY, tsec_api.FALSE_NEGATIVE_RETRIES = 1, 0.0, 0
    env_fast = {**env, "SUBMIT_ATTEMPTS": "1", "SUBMIT_BASE_DELAY": "0", "FALSE_NEGATIVE_RETRIES": "0"}
    pending = Path(tmp) / "logs" / "pending_flags.jsonl"
    BOARD_ONE = "web_idor_02"
    GOOD_ONE = "flag{idor_1}"

    mock_busy([GOOD_ONE])
    tsec_api.start_challenge(TOK, BOARD_ONE)
    pbusy = subprocess.run([sys.executable, str(ROOT / "tools" / "submit_flag.py"),
                            BOARD_ONE, GOOD_ONE], capture_output=True, text=True, env=env_fast)
    check("平台没受理时返回 SUBMIT_BUSY(退出码 3,不记 WRONG)",
          pbusy.returncode == 3 and "SUBMIT_BUSY" in pbusy.stderr, pbusy.stderr[:120])
    rows = [json.loads(x) for x in pending.read_text(encoding="utf-8").splitlines() if x.strip()]
    check("没受理的值进了待办(题号 + flag 原样记下)",
          len(rows) == 1 and rows[0]["question_id"] == BOARD_ONE and rows[0]["flag"] == GOOD_ONE,
          str(rows)[:160])

    plist = subprocess.run([sys.executable, str(ROOT / "tools" / "backfill_flags.py"), "--list"],
                           capture_output=True, text=True, env=env_fast)
    check("backfill --list 能列出待办", GOOD_ONE[:10] in plist.stdout, plist.stdout[:120])

    mock_busy([])                                  # 通道恢复
    c = backfill_flags.run_once(log=lambda *a: None)
    check("通道恢复后补交成功", c["ok"] == 1 and not c["wrong"], str(c))
    check("待办被清空(自清理)", not pending.read_text(encoding="utf-8").strip())
    got = [q for q in tsec_api.list_questions(TOK) if q["question_id"] == BOARD_ONE][0]
    check("平台侧真的计上了这个 flag", int(got["correct_flag_count"]) == 1, str(got)[:140])
    sublog = (Path(tmp) / "logs" / "submissions.log").read_text(encoding="utf-8")
    check("补交在 submissions.log 里标了 by=harness(审计能分清谁交的)",
          '"by": "harness"' in sublog, sublog[-160:])

    # 边界:WRONG 永不进待办、永不被补交
    pwrong = subprocess.run([sys.executable, str(ROOT / "tools" / "submit_flag.py"),
                             BOARD_ONE, "flag{not-the-answer}"],
                            capture_output=True, text=True, env=env_fast)
    check("受理但判错仍是 WRONG(退出码 1)", pwrong.returncode == 1, pwrong.stdout[:120])
    check("WRONG 不进待办(判错 ≠ 通道问题)", not pending.read_text(encoding="utf-8").strip())
    env_bad = dict(os.environ, CONTEST_ADAPTER="tsec", ROTOM_RUN_DIR=tmp, BENCHMARK_TOKEN="")
    env_bad.pop("TEAM_TOKEN", None)
    pnotok = subprocess.run([sys.executable, str(ROOT / "tools" / "backfill_flags.py")],
                            capture_output=True, text=True, env=env_bad)
    check("没有 token 时补交不抛异常、只是跳过", pnotok.returncode == 0, pnotok.stderr[:120])
    tsec_api.FALSE_NEGATIVE_RETRIES = 2            # 还原,别影响后面

    # runner 的接线:节流开关能整体关掉;打开时真的会补一轮(被受理但判错即丢弃)
    backfill_flags.save_pending([{"ts": "t", "question_id": BOARD_ONE, "flag": "flag{x}",
                                  "reason": "限流", "attempts": 0}])
    runner.BACKFILL_INTERVAL, runner._backfill_last = 0, 0.0
    runner.maybe_backfill_flags()
    check("BACKFILL_INTERVAL=0 → runner 不补交(补交可整体关闭)",
          len(backfill_flags.load_pending()) == 1, str(backfill_flags.load_pending()))
    runner.BACKFILL_INTERVAL, runner._backfill_last = 300, 0.0
    runner.maybe_backfill_flags()
    check("runner 接线打通:通道可用时补一轮,被受理但判错即丢弃",
          len(backfill_flags.load_pending()) == 0, str(backfill_flags.load_pending()))

    # ---------------------------------------------------------------- get_hint 工具(agent 自主取提示)
    section("get_hint 工具(agent 自主)")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "get_hint.py"), "web_sql_injection_01"],
                       capture_output=True, text=True, timeout=30)
    check(r.returncode == 0 and "/api" in r.stdout and "/admin" in r.stdout,
          "CLI 取回 mock 平台提示原文", (r.stdout + r.stderr)[:120])
    with board_mod.locked() as b:
        facts = [f["text"] for f in b.get("facts", [])]
    check(any("agent 主动取了 web_sql_injection_01" in x for x in facts), "取回写黑板 fact(审查可对账)")
    r2 = subprocess.run([sys.executable, str(ROOT / "tools" / "get_hint.py")],
                        capture_output=True, text=True, timeout=30)
    check(r2.returncode == 2, "缺参数 → 退出码 2(用法错误)")

    print(f"\n{'='*50}\n通过 {PASS} 项,失败 {FAIL} 项")
    if FAILED:
        print("失败项:")
        for f in FAILED:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
