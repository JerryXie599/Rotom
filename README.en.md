<div align="center">

<img src="docs/logo.png" alt="Rotom — CTF AI Agent System" width="360"/>

# Rotom ⚡

**Autonomous CTF-solving agent — it slips into a challenge and takes its shape**

[简体中文](README.md) · [English](README.en.md)

</div>

---

> "With its ion-powered body, it slips into machines and makes them its own vessel."
> — [Pokémon百科 · Rotom](https://wiki.52poke.com/wiki/%E6%B4%9B%E6%89%98%E5%A7%86) (translated)

**Rotom** (National Dex No.479) is an Electric/Ghost "Plasma Pokémon" whose name is **MOTOR spelled backwards**.
It possesses different appliances and changes form with its host — oven, washing machine, refrigerator, fan, lawn mower:
five forms, each with its own typing and moves. Once it possesses a Pokédex, it becomes the talkative **Rotom Pokédex** that assists its trainer.

That is exactly what an autonomous CTF-solving agent should be: **one skeleton, taking the shape of whatever challenge it slips into**.
Web / Pwn / Reverse / Crypto / Forensics each get their own playbook and toolkit, while scheduling, concurrency, coordination,
auditing and supervision are shared skeleton. And like the Rotom Pokédex, it works fully on its own while narrating every step in real time.

> Architecture deep-dive (diagrams / sequence / state machine): [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## What it is

A lightweight harness for "AI agent solving contest" style events, inspired by [Cairn](https://github.com/oritera/Cairn)
(blackboard + isolated workers + origin/goal tasks):

- The **scheduler never touches the challenges**: it polls the platform, dispatches breadth-first, controls concurrency, retries on timeout, and reconciles with the platform;
- The **solving layer** runs one isolated agent process per challenge (a local `pi` CLI worker) with its own workspace, attachments, shared blackboard, and per-category knowledge base;
- **Flag submission and bookkeeping** go through harness tools only, so the audit trail is complete and exportable.

Model-agnostic: any OpenAI-compatible / Anthropic / Google endpoint works. On contest day you paste the base URL + key into the web console — no command line required.

## Battle record

| Scenario | Result |
|---|---|
| Official round 2 (10 challenges: web×3 / pwn×2 / reverse×2 / misc×2 / crypto×1) | **10/10 solved in 10 min 09 s**, first flag 18 s after start, 10/10 correct submissions |
| Official round 3 (finals, markedly harder) | **6/10**, zero wrong submissions; the 4 unsolved were all long-chain web/heap challenges |
| Local load test, 14 challenges × 36 workers | **14/14 solved**, avg 67 s (fastest 19 s), zero rate-limit degradation |
| Local 5-category acceptance (web/pwn/reverse/crypto/forensics) | **5/5 in ~3 minutes**, 100% submission accuracy |
| NSSCTF Agent Arena (60-min window) | 12/13 in a single round; cumulative **21/22 = 95.45%**, rating 1432 → 1759 |

## Quick start (two steps on contest day)

```bash
./start.sh                 # one shot: pre-flight checks → web console → start solving in the background (auto-restart on crash)
```
or **double-click `启动Agent.command`** (equivalent, and opens the browser for you).

It prints this round's deadline, e.g. `本轮截止 22:00:34(剩余 59 分 56 秒)` — retries are unlimited until the deadline, then it winds down by itself.

| Command | Purpose |
|---|---|
| `./start.sh` | One-shot start (checks / console / run; `--supervise` crash auto-restart by default) |
| `./start.sh --check-only` | Pre-flight checks only |
| `./start.sh status` / `./start.sh stop` | Status / graceful shutdown (stops supervisor and every agent) |
| `./start.sh --practice` | Practice mode (re-solve challenges the platform already marks solved) |
| `./start.sh --project X` / `--workers N` | Pick project / override concurrency |
| `./start.sh --no-supervise` | Disable crash auto-restart |

**Model config**: the four `.env` keys `PI_BASE_URL / PI_API_KEY / PI_MODEL / PI_API_TYPE` (or edit them in the web console).
Switching to the organizer's model = edit four fields → save → click "Test model".

**Round timer**: counted from the moment you press "start"; `ROUND_WINDOW_MINUTES=60`, or an absolute `ROUND_END_AT=14:00`.
The deadline is persisted — **restarts do not reset it**, but an **expired deadline starts a fresh round** (so it never gets stuck overnight).

## How it solves (fully autonomous once started)

1. **Start wide**: `START_WORKERS` (default 36, tune to the model's quota) workers run in parallel, dispatched **breadth-first** across challenges;
2. **Same-challenge agents coordinate on a shared blackboard**: `work/<challenge>/SHARED.md` is append-only; later agents read it first, avoiding duplicated work and building on teammates' breakthroughs;
3. **Auto-degrade on rate limits**: when worker logs hit rate-limit signatures, the concurrency cap drops **20% per step** (30 s cooldown, floor `MIN_WORKERS=4`): 36→29→24→20→16→13→11→9→…→4. Rate-limited workers don't burn retry budget;
4. **Solve → submit → move on**: the agent calls the submit tool; once the platform confirms, all workers on that challenge are killed and the rest continue. When everything is done it stays resident polling for new challenges (`KEEP_RUNNING=1`);
5. **Unsolved challenges keep reviving**: time-bounded unlimited retries (see below); each retry auto-injects the most relevant knowledge-base excerpts.

## Concurrency & on-demand reinforcement (agents can't spawn processes; the harness dispatches)

- Default **2 agents per challenge**; an agent that needs more firepower writes one line to the blackboard:
  `echo "- [wN 请求增援] 需要 2 个 agent:一个爆破 key 区间,一个扫 /admin" >> SHARED.md`
  The runner parses it and **only if slots are free** dispatches helpers (capped by `MAX_HELPERS_PER_QUESTION=4`), injecting the requested division of labor into their prompts. Each request is granted once (blackboard bookkeeping).
- **Agents should not spawn pi processes themselves**: that bypasses concurrency control (blows the model quota), submission bookkeeping (blackboard goes stale), and scatters the audit trail. The harness is the only dispatcher.
- The prompt also teaches agents to parallelize their own work (`xargs -P`, background jobs with timeouts) — extra agents are rarely needed.

## Retries & the time boundary (time-bounded, no attempt cap)

- The clock starts when you press "start"; **retries are unlimited within the window** — timed-out / crashed / exited workers are re-queued immediately (5 s backoff after instant exits). There is no "give up after N tries";
- **Hard stop** at `ROUND_WINDOW_MINUTES`: all workers killed, no new dispatch in the final 2 minutes (`STOP_SPAWN_BEFORE_END`);
- The **deadline is persisted** (`logs/run_deadline.json`): crash-restarts and console restarts keep the same deadline instead of re-timing the round;
- **Per-challenge timeout escalates with attempts**: 420 → 540 → 660 → 780 → 900 s (`TIMEOUT_ESCALATE=120`, `MAX_WORKER_TIMEOUT=900`), so one hard challenge can't eat the whole round;
- Model-side limits don't consume retries: rate limits degrade concurrency; quota exhaustion pauses dispatch for `QUOTA_BACKOFF` seconds.

## Architecture

![Rotom architecture: runner dispatch loop → one isolated solver process per challenge → unified submission and blackboard](docs/architecture.png)

- `contest_api.py` — wraps the three platform endpoints (list / reset / submit); submission has **built-in rate-limit backoff** and distinguishes "wrong answer" from "not accepted"
- `board.py` — the blackboard; persisted to `board.json`, flock-safe; challenge state machine pending→running→solved/failed
- `solver.py` — per-challenge workspace, downloads/unpacks attachments, assembles the prompt (category playbook + KB excerpts + time budget), launches pi;
  also hosts the failure classifier (timeout / rate limit / quota / network), which **requires error-semantics context** so the model's own prose can't be mistaken for a server error
- `runner.py` — main loop: 3 s polling, breadth-first dispatch, 2 agents per challenge by default, per-challenge timeout from 420 s escalating, time-bounded unlimited retries, platform reconciliation
- `tools/submit_flag.py` — submission tool called via bash; three verdicts: `CORRECT` / `WRONG` / `SUBMIT_BUSY` (rate-limited — retryable, does NOT count as wrong)
- `tools/reset_env.py` — resets containerized challenge environments
- `dashboard.py` — local web console (stdlib only), shows what every agent is doing right now
- `tools/monitor.py` — terminal monitoring, for when there's no browser

## Projects (one run = one project, fully isolated)

Each project has its **own data directory**: its own challenge state (`board.json`), workspace (`work/`), logs (`logs/`) and its own runner process.
Run several projects side by side without overlap or competition.

- **Default project**: the repository root (backwards compatible with running from the CLI);
- **New project**: type a name on the overview page, data lands in `projects/<name>/`.

```
<project root>/
  board.json  work/  logs/      # default project
  projects/
    round1/                     # a new project
      board.json  work/  logs/  meta.json
    round2/
```

Model, token and platform endpoints are configured once in the console and **shared across projects**;
project-level knobs (concurrency, timeouts) currently share the same `.env`.

## Web console (config + start/stop + live view)

```bash
./dashboard.sh                 # default http://127.0.0.1:8799 (auto-increments if taken; prints the actual URL)
```

The control panel has three sections, all editable:

- **Platform**: team token, base URL, and the three endpoint paths (list / reset / submit — full URLs also accepted)
- **Model**: base URL, API key, model name, API type (dropdown, includes `anthropic-messages`)
- **Runtime**: starting concurrency, contest/practice mode, **network mode (`direct` / `system` proxy)**

Buttons: Save / Test platform / Test model / Start / Stop / Restart. Saving writes `.env` and syncs the model config —
zero command line on contest day.

- "Test platform" really lists challenges once and tells you the count or the exact error;
- "Test model" runs one real model call to confirm the endpoint works;
- "Stop" gracefully reaps every agent (no orphans); "Restart" = stop + start; model/token changes need a runner restart (the page reminds you).

The **live view** shows per-challenge status badges, each agent's **real-time tool calls and speech**, token usage,
silence duration (>180 s flagged "possibly stuck"), the shared blackboard, submissions, and an event stream.
Data comes straight from `board.json` / `logs/*.jsonl`; zero dependencies, offline, listens on 127.0.0.1 only.

> Why not Langflow: it's a "build an LLM workflow" tool that needs its own stack (Docker/Node) and shows flowcharts,
> not live agent state. This console grows on the harness and starts with it. For cross-session LLM tracing/cost, look at Langfuse or Arize Phoenix.

## Configuration (two entry points, effective immediately)

**1. Team token & platform endpoints**:

```bash
python3 config.py --token icqXXXXXXXXXXXXXXXX
python3 config.py --contest-base https://<organizer-domain> \
                  --contest-query-path /query --contest-reset-path /reset --contest-submit-path /submit
```

The web console is the recommended path — click "Test platform" to verify instantly. Paths can be relative (appended to Base)
or a full URL pasted verbatim; empty fields fall back to built-in defaults.

**2. Model (base URL / key / model / API type)** — switching to the organizer's model is one command:

```bash
python3 config.py --base-url https://api.xxx/v1 --key sk-xxx \
                  --model deepseek-flash --api-type openai-completions
python3 config.py --test        # one real call to verify connectivity
python3 config.py               # show current config (key masked)
```

`--api-type` is one of (pi provider API types):

| API type | Good for |
|---|---|
| `openai-completions` | OpenAI Chat Completions — **the most universal**, works with most relays and Chinese models |
| `anthropic-messages` | Anthropic Messages API (Claude, or relayed anthropic endpoints) |
| `openai-responses` | OpenAI Responses API |
| `google-generative-ai` | Google Generative AI |

You can also edit the four `.env` lines directly — the harness syncs them into pi's `models.json` (a dedicated provider entry) on startup.

> **Networking (learned the hard way)**: with `NET_PROXY=direct` (default) the harness **strips** `http_proxy`-style variables —
> when a local proxy client dies (or the venue has none), requests carrying a dead proxy fail instantly with `Connection error`;
> direct mode is immune. Platform domains are always direct (a local proxy breaks their HTTPS). Set `NET_PROXY=system` only if you truly need a proxy.

## Workspace layout

One directory per challenge, named **`<category>-<name>`** (e.g. `web-web01`):

```
work/
  web-web01/          # challenge workspace
    .qid              # challenge ID marker (dedupes same-named challenges)
    SHARED.md         # shared blackboard for all agents on this challenge
    files/            # unpacked attachments (read-only reference)
    w1/  w2/          # per-agent working directories
  misc-misc01/
  pwn-pwn01/
```

**Retries preserve the scene**: when a worker is reaped, its directory and blackboard are kept — the retrying agent reads its
predecessor's notes before acting. Measured on two hard challenges: attempt 1 ran out its 420 s, the successor finished in
**40 s / 63 s** off those notes.

## CTF knowledge base (offline, queryable by agents)

Everything lives on disk under `knowledge/` (no internet at the venue), with a search tool agents call directly:

```bash
python3 tools/kb.py list                          # what's in the KB
python3 tools/kb.py search "sql 注入 绕过"        # keyword search (CN/EN, multi-term = AND)
python3 tools/kb.py show pwn                      # full playbook for a category
python3 tools/kb.py grep "0x67452301"             # literal search for constants/signatures
```

- Index: built and cached on first search, rebuilt automatically when sources change; 9000+ chunks in 0.2–0.3 s.
- Content: `knowledge/{web,pwn,reverse,crypto,forensics,misc}.md` (hand-written playbooks: approaches, pitfalls, speed tips)
  + `vendor/PayloadsAllTheThings` + `vendor/ctf-wiki` + `vendor/RsaCtfTool` — 900+ documents in total.
- Third-party material is **not in the git repo**; run `knowledge/fetch_vendor.sh` after cloning to rebuild it.

## How the KB actually gets used (four layers, measured)

A knowledge base nobody queries is furniture, so there are four layers, each less dependent on the model's initiative:

1. **Category playbook injected into every prompt** — common challenges don't need a lookup at all;
2. **Agent-initiated search** via `tools/kb.py search/show/grep`;
3. **Forced lookup when stuck**: the prompt's code of conduct requires a KB check after 8–10 steps without progress, and the runner-side Observer writes reminders into the blackboard the agent must read;
4. **Harness-fed retries**: from attempt 2 on, the runner searches with the challenge's keywords and injects the top excerpts, labeled "previous attempt failed — start here".

**Measured usage** (counting `kb.py` calls in real run logs):

| Scenario | Agent sessions | KB lookups | What triggered them |
|---|---|---|---|
| Arena (unfamiliar challenges) | 10 | **18** | file-server path tricks, unpacking ASPack, unicorn-based emulation — long tail the playbook didn't cover |
| Local load test (forensics/pwn) | 8 | 0 | the category playbook was already injected |

Conclusion: agents **do search when facing unfamiliar territory** and rely on injected playbooks otherwise; with retry-feeding as the safety net, the KB no longer depends on the model remembering to look. Gaps found during testing were written back into the playbooks.

## Contest-day ops tools (forged in real rounds)

| Tool | Purpose |
|---|---|
| `python3 tools/watchdog.py --project X` | **Resident supervisor**: 15 s health checks; restarts the console or the runner if they die; kills+relaunches a runner whose heartbeat stalls 180 s; before the round, probes every 45 s for early challenge release; exits when the window ends |
| `python3 tools/status_now.py --project X` | One-screen status: window remaining, concurrency, per-challenge attempts, submission verdicts (CORRECT/WRONG/not-accepted), error counts |
| `python3 tools/round_watch.py --project X` | Event sentinel: silent until a new challenge appears / the runner dies / the window ends, then exits to wake the operator |
| `python3 tools/postmortem.py --project X` | **Post-round analysis**: per-session tool calls / turns / tokens / compactions / errors, bash commands classified (pure navigation, repeats, web attempts, tool hunting) — measure, don't guess |
| `tools/bin/timeout` | macOS ships no GNU `timeout`; the harness carries a shim and injects it into workers' PATH so agents can bound long commands |

**Two harness defects found and fixed mid-round** (why these tools exist):

1. **Failure classification fooled by the model's own prose**: the classifier greps worker logs, which contain the model's thinking —
   a crypto agent wrote "the modulus n is not **balanced**", the bare word `balance` matched, and the harness declared "quota exhausted",
   pausing all dispatch for 900 s. Fix: only full error-semantic phrases, and the keyword must co-occur with an error marker (unit-tested).
2. **Unbounded commands eating the window**: macOS lacks `timeout`, so agents' `timeout 60 nmap ...` all failed with `command not found`
   and a bare `nmap -p-` hung for 8 minutes. Fix: ship the `timeout` shim + a prompt rule that long commands must be bounded.

## When there are many challenges (dozens per round)

The scheduler is breadth-first by design and never piles concurrency onto a few challenges:

- **Cover first, deepen later**: with N challenges and cap M, dispatch 1 agent to M challenges; only add a second agent when challenges < slots;
- **Fair queueing**: new challenges and fewer-attempted challenges first — nothing starves;
- **Parallel attachment prep**: downloads/unpacking run in a thread pool (300 s timeout releases the slot);
- **Attempts only count on real launch**: prep failures and crashes don't burn retries;
- **Log auto-cleanup**: `logs/` beyond `MAX_LOG_MB` prunes oldest (disabled in audit mode, see below).

The only knob that matters when challenges are many is the **per-challenge timeout**; the runner prints a hint when challenges > 2× concurrency.

Scheduling regression (no model calls, no network):

```bash
python3 tests/scale_test.py 30     # also try 24 / 60 / 100
```

## Local toolchain

- [pi CLI](https://github.com/earendil-works/pi) (model access layer; a worker is a `pi --mode json` headless process);
- Python 3.10+ (stdlib only, no pip installs);
- macOS built-ins (python3/curl/nc) work; recommended solving tools: pwntools, gdb, binwalk, tshark; containerized challenges via an OrbStack Linux VM (`orb`);
- `timeout` is bundled by the harness on macOS (see above) — coreutils not required.

## Delivery & audit (contest requirement)

The rules require handing the agent to organizers for re-testing and process audit after the round: a deployment package whose README documents
"environment & dependencies / configuration & startup / logs & audit paths". This is built in:

```bash
python3 tools/export_trace.py         # convert logs/*.jsonl into readable Thought/Action/Observation traces -> traces/
python3 tools/package_submission.py   # package the deployment bundle into dist/ (README + traces, auto-redacted)
python3 tools/package_submission.py --zip            # also produce .zip
```

- Trace export turns machine jsonl into human-readable reports (`traces/<challenge>_w<slot>.md` + `SUMMARY.md`);
  audit mapping: tool call=`tool_execution_start` (Action), tool output=`toolResult` (Observation), model speech=`message_end` (Thought);
- **Logs are not auto-pruned** by default (`MAX_LOG_MB=0`) — audits need the full trail; set `MAX_LOG_MB` if disk gets tight, but run `export_trace.py` first;
- Packaging auto-redacts secrets (placeholders) and only ships `.env.example`.

## Tests & benchmarks (four layers, none need the real platform)

| Script | Purpose | Burns model quota |
|---|---|---|
| `python3 tests/unit_test.py` | **58 offline assertions**: round window, degradation steps, submission verdicts (rate-limit ≠ wrong), classifier context gate, network policy, blackboard state machine, prompt assembly | no |
| `python3 tests/scale_test.py 30` | Scheduling regression: breadth-first, concurrency cap, no starvation, no attempt leaks (24/30/60/100 challenges) | no |
| `./tests/local_contest.sh start` | Local end-to-end: isomorphic mock platform + pwn service + real agents (small scale) | yes |
| `./tests/load/run_load.sh 24` | **High-concurrency load test**: 14 challenges × 24 workers, capacity & timing report | yes |

### Measured capacity (M3 Mac / DeepSeek Flash / 24 workers)

- 12 forensics challenges all solved, avg 90 s (48–138 s), one submission each, **0 wrong submissions**; 2 pwn copies went into long-running solves;
- Peak **26 pi processes / 4.0 GB RAM**, runner CPU <1% — the bottleneck is the model and the challenges, never the harness;
- Zero runner exceptions across the whole run.

## Runtime robustness

- **Crash auto-restart**: `--supervise` (relaunch 5 s after abnormal exit; a clean exit stays down);
- **Heartbeat monitoring**: the runner writes `logs/runner_state.json` each loop; the console flags "runner unresponsive" after 60 s;
- **Window backstop**: auto wind-down at the deadline; no new dispatch in the last 2 minutes;
- **Orphan cleanup**: stale agent processes from a previous run are reaped (by verified process name) at startup;
- **Rate-limit immunity**: platform throttling ("too frequent") is retried with backoff and **never recorded as a wrong answer**;
- **Classifier anti-false-positive**: rate/quota/network verdicts require keyword + error-context to co-occur — model prose can't trigger global actions;
- **Self-healing directories**: missing data dirs are recreated; **pre-flight**: `./start.sh --check-only` checks python/pi/tshark/orb/KB/disk/platform.

## Acknowledgements

- [Cairn](https://github.com/oritera/Cairn) — architectural inspiration: blackboard coordination and origin/goal tasks;
- [pi](https://github.com/earendil-works/pi) — model access layer and agent runtime;
- [PayloadsAllTheThings](https://github.com/swisskyrepo/PayloadsAllTheThings) / [ctf-wiki](https://github.com/ctf-wiki/ctf-wiki) / [RsaCtfTool](https://github.com/RsaCtfTool/RsaCtfTool) — offline knowledge base and tooling (rebuilt via `knowledge/fetch_vendor.sh`, not distributed in-repo);
- Rotom is a trademark of Nintendo / Game Freak / The Pokémon Company. This project is not affiliated with or endorsed by them — just a tribute to the little plasma ghost that possesses machines and takes their shape.
