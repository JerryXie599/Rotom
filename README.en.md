<div align="center">

<img src="docs/logo.png" alt="Rotom — CTF AI Agent System" width="360"/>

# Rotom ⚡

**Autonomous CTF-solving agent — it slips into a challenge and takes its shape**

[简体中文](README.md) · [English](README.en.md)

</div>

---

> "With its ion-powered body, it slips into machines and makes them its own vessel."
> — [Pokédex · Rotom](https://wiki.52poke.com/wiki/%E6%B4%9B%E6%89%98%E5%A7%86)

A lightweight harness for "AI agent solving contest" events, inspired by the blackboard architecture of
[Cairn](https://github.com/oritera/Cairn): **the scheduler only dispatches, throttles, retries and reconciles —
each challenge gets one isolated agent** (a `pi` CLI worker) with its own workspace, shared blackboard and per-category
knowledge base. Submission and audit go through harness tools only. Model-agnostic — paste a base URL + key in the web
console and go.

## Battle record

| Scenario | Result |
|---|---|
| Official round 2 (10 challenges) | **10/10 in 10 min 09 s**, first flag at 18 s, all submissions correct |
| Official round 3 (finals, harder) | 6/10, zero wrong submissions |
| Local 14 challenges × 36 workers | 14/14, avg 67 s, zero rate-limit hits |
| 5-category full-auto acceptance | 5/5 in ~3 min |
| NSSCTF Agent Arena | cumulative **21/22 = 95.45%** |

## Quick start

```bash
./start.sh                   # pre-flight checks → web console → run in background (auto-restart on crash)
./start.sh --check-only      # checks only
./start.sh status | stop     # status / graceful shutdown
```

On contest day: fill **token + model fields** (base URL / key / model / API type) in the console → "Test platform" /
"Test model" → Start. The round clock starts then (`ROUND_WINDOW_MINUTES`); the deadline is persisted across restarts
and re-times automatically once expired.

## How it solves

**Breadth-first** across challenges → 2 agents per challenge coordinating on a **shared blackboard** → concurrency
auto-degrades 20%/step on rate limits → per-challenge timeout escalates 420→900 s with **time-bounded unlimited retries**
(scene preserved, knowledge auto-fed) → solve, submit, move on → stay resident for new challenges.
Need more firepower? An agent writes a reinforcement request on the blackboard; the harness dispatches helpers when
slots are free (max 4 per challenge).

## Architecture

![Rotom architecture](docs/architecture.png)

`contest_api.py` list/reset/submit (rate-limit backoff; "wrong" vs "not accepted" distinguished) ·
`solver.py` workspace/attachments/prompt/failure classifier · `runner.py` main loop · `board.py` blackboard ·
`dashboard.py` web console · `tools/` submit, reset, KB search, ops.
Full design (diagrams / sequence / state machine): **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**

## Configuration

```bash
python3 config.py --token icqXXX \
                  --base-url https://api.xxx/v1 --key sk-XXX \
                  --model deepseek-flash --api-type openai-completions
python3 config.py --test          # verify connectivity
```

Or do it all in the web console. Networking defaults to **direct** (`NET_PROXY=direct`), immune to dead local proxies;
OpenAI-compatible / Anthropic / Google API types are supported.

## Offline knowledge base

9000+ chunks, 0.2 s search: six hand-written playbooks + PayloadsAllTheThings + ctf-wiki + RsaCtfTool.

```bash
python3 tools/kb.py search "sql 注入 绕过"
```

Playbooks are injected into prompts, stuck agents are forced to search, retries get auto-fed — four layers make sure
the KB is actually used. Third-party material is not in the repo; run `knowledge/fetch_vendor.sh` to rebuild it.

## Contest-day ops

`tools/watchdog.py` self-healing supervisor (restarts console/runner, kills stalled heartbeats, probes for early
challenge release) · `tools/status_now.py` one-screen status · `tools/round_watch.py` event sentinel ·
`tools/postmortem.py` post-round analysis (measure, don't guess).

## Tests (none need the real platform)

`tests/unit_test.py` **58 offline assertions** · `tests/scale_test.py` scheduling regression (24–100 challenges) ·
`tests/local_contest.sh` local end-to-end · `tests/load/run_load.sh` high-concurrency load test.

## Delivery & audit

`tools/export_trace.py` exports readable Thought/Action/Observation traces; `tools/package_submission.py` packages the
deployment bundle (auto-redacted, compliant README included).

## License

**Fork and modify freely**; for **commercial use**, contact the author: 1054544881@qq.com

## Acknowledgements

[Cairn](https://github.com/oritera/Cairn) (architecture) · [pi](https://github.com/earendil-works/pi) (runtime) ·
[PayloadsAllTheThings](https://github.com/swisskyrepo/PayloadsAllTheThings) / [ctf-wiki](https://github.com/ctf-wiki/ctf-wiki) /
[RsaCtfTool](https://github.com/RsaCtfTool/RsaCtfTool) (knowledge base).
Rotom is a trademark of Nintendo / Game Freak / The Pokémon Company; this project is not affiliated with them — just a
tribute to the little plasma ghost that possesses machines and takes their shape.
