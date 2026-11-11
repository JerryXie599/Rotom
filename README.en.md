<div align="center">

<img src="docs/logo.png" alt="Rotom" width="300"/>

# Rotom

**A general-purpose AI solving agent: CTF contests · vulnerability benchmarks (TSec) · authorized pentest**

</div>

---

`Rotom` is a **strategy-switchable AI solving agent platform**: scheduling, blackboard, knowledge base,
web console and audit traces are fully shared across all three scenarios; only the host adapter layer is
swapped (`tsec_api.py` / `contest_api.py` / `arena_api.py` / `targets_api.py`).
Full file map: [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md). Version history: git commit log.

> The current main branch is `Rotom_beta` (version-number naming); the next release will be pushed to GitHub.

## Five profiles: CTF / TSec / Pentest / Fraud-site recon / NewRank

One mechanism, five **strategies** switched by data (`PROFILE=ctf|zhongce|pentest|shezha|newrank`,
also selectable when creating a project in the web console):

| Profile | Scenario | Definition of done | Task source | Containers |
|---|---|---|---|---|
| `ctf` | CTF contest | `CORRECT` submit | platform adapter | platform-managed |
| `zhongce` (default) | TSec / crowd-test benchmark | `本题通关 x/x` | platform adapter | harness-managed, slot rotation |
| `pentest` | authorized pentest engagement | `report_finding --done` | local `targets.json` | client environment |
| `shezha` | fraud-site reconnaissance (automated batches) | `--done` + archive completeness gate | local `targets.json` (sliced from Excel lists) | none (probe via proxy) |
| `newrank` | 2026 national AI-offense contest (NewRank platform) | `Correct` submit | platform `getquestion/submitflag` | none (direct web targets) |

**NewRank has two platform quirks**: ① every question URL carries a **single-use** `__ctf_ticket`;
the first GET returns 303 + `Set-Cookie` and the ticket dies. The adapter exchanges the ticket for a session
Cookie while fetching questions and writes a Netscape `cookies.txt` into each worker's directory (the prompt
requires curl/dirsearch/sqlmap/ffuf/requests to send it). ② the submit response has no `correct_flag_count`,
so the adapter fills 1/1 to let the submit tool mark questions solved.
**AI call monitoring**: `tools/pi_extensions/ai_trace.ts` records start / time-to-first-byte / end and usage of
each model call into `<worker-log>.calls`; `tools/ai_monitor.py` aggregates them into `logs/ai_usage.json`.
The console shows total tokens, tokens/min and average latency in the top bar ("AI"), with a per-question
breakdown on click. `ROTOM_AI_TRACE=0` disables it.

Prompts / red lines / toolchain / playbook live in `prompts/<包>/` and `profiles/<包>.json`
to change wording, no code edits). Each profile also provides a **pi system prompt**
(`--append-system-prompt`; `ctf`/`zhongce` share one, `pentest` has its own that includes
an authorization declaration).
**The fraud console grades outcomes itself**: the AI reads every site archive and assigns a tier —
solid / partial / **needs human review** / recon only / no material. Evidence that merely *proves the site
is legitimate* (ICP filing numbers, business licences) is **not** counted as a result: such sites land in
"needs human review", where one click on the site page either stops them (added to the do-not-touch list)
or keeps going. A site that has been **concluded** (not fraud / work delivered) can be **archived**:
it leaves the working sidebar and no longer gets dispatched, with per-site, per-cluster and
"all pending" entry points, a dedicated "已归档 N" chip to review them, and per-site undo
(source of truth: `archive` in `config.json`). The sidebar lists results first, sorted by tier; upgrading the grading criteria re-reads
old verdicts automatically (each verdict carries a `REV` stamp — deep/mid/legit are re-read, surface/none pass through free).
**Site pages follow the unified per-site tree**: 报告/总报告.md (the single full report) · timeline ·
mind map (source + "open map") · archive page · folder inventory (evidence / delivery / large evidence /
sentry scripts / stray files); dot-less cluster-level folders are part of the tree too.
**pi extensions (`extensions` field in a profile json)**: the `shezha` profile loads
`tools/pi_extensions/egress_guard.ts`, turning "all probes must go through the proxy" into
**program enforcement** — every bash command is checked against a policy script (pointed to by
`EGRESS_GUARD_SCRIPT`; same hook protocol as the fraud repo's ZCode hook); commands without
proxy traces are blocked before execution and the compliant rewrite is fed back to the model;
fail-open.
**The loop is task-driven, not clock-driven**: for benchmark engagements it stops only on four conditions
(all tasks done / unrecoverable platform state / network down repeatedly / invalid token), and time is used for
budgeting alone; chained multi-flag tasks get a pinned container slot and per-difficulty session budgets live in
`profiles/zhongce.json` (`loop` / `budgets`).
**Submissions retry false negatives on the same string, and direct mode now bypasses the macOS system proxy** for
agents' own scripts; point `kb_extra` in `profiles/zhongce.json` at last round's `solve/`+`ref/` and the next round
can search those per-task conclusions (the biggest cross-round lever we measured).
**Containers are health-probed at the service level**: the platform reporting `available` does not mean the app is
alive (a socket that accepts TCP but never answers HTTP counts as dead) — a few consecutive failed probes make the
harness close and replace it, with no human rescue needed. In the endgame (one or two tasks left, free slots idle)
stuck tasks automatically get reinforcement agents.
**Progress nudges are outcome-based, not step-based**: the Observer speaks only when an agent repeats the exact same
command or when nothing new has been proven for a long stretch (edge-triggered, cooldown, per-task cap), and never
while the container is down — long exploit chains no longer get interrupted by "N steps without a submission".
**The knowledge base ships a CVE pack**: `python3 tools/kb.py hit "comfyui"` / `hit "grafana 8.2"` lands on a
component card (`knowledge/cves/`: classic middleware + AI/ML services + token/sandbox attacks, public advisory
anchors plus the shortest verification path; each card's CVE table is graded `verified / advisory / candidate`,
and candidates must be confirmed upstream before use — always confirm the version first).
**Credential-work kit (vendor dictionaries + product-word×year generator)**: the most common blocker in a scoring run is
not the exploit chain but a node where *every* entry point needs authentication. The image ships **six vendor-scoped
password tables** (Weaver / domestic OA / network appliances / surveillance IoT / ops & middleware / generic weak base,
435 entries) plus a vendor→product-word vocabulary and a username face; `python3 tools/gen_product_year.py --vendor 泛微`
generates the "product word × year" family on the fly from the fingerprint (`weaver@2023`, `Ecology@2024` — a pattern
generic dictionaries can never cover); `tools/dict_prep.py` assembles the list in a fixed order —
**vendor-scoped → credentials already observed on this target → year family → generic high-frequency, reordering not
supported** (auth services are almost always hard rate-limited, so order *is* the strategy). The full playbook
(judge the channel first → measure the rate-limit window → paced delivery, including the "concurrent brute force killed
the very service we needed" incident) is `knowledge/playbooks/credential-wall.md`.
Scope: public weak/default password content only — **no challenge-bound answers**.
**Both the adapter and the token resolve as "env var → `.env` → explicit failure"**: if `.env` is not exported into the
hosted container, the old build *silently* fell back to the CTF adapter (legacy domain) and voided the entire submission
stream (4h40m / 250+ wasted in the 6th run). That can no longer happen — regression asserts live in
`tests/hosted_pack_test.py`.
**Harness backfills submissions the platform never accepted**: a flag value an agent extracted and tried to submit, but which the platform reported as not accepted (SUBMIT_BUSY / SUBMIT_ERROR), is recorded and resubmitted verbatim once the channel recovers (`tools/backfill_flags.py`; logged with `by: harness`). Prompts forbid agents from writing their own retry loops (one burned hours retrying every 20s in the 6th run); `WRONG` is never backfilled or retried.
**Ships as a platform-hosted Docker image**: `bash docker/build.sh` → `dist/rotom-agent.tar.gz`. The build rewrites the
model endpoint to the platform gateway (`<domain>.tsecbench.gw`), bakes in every tool plus the offline knowledge base so
the agent works with no internet at all, and `docker/selfcheck.sh` asserts the image carries **no per-task answer memory,
no answer-shaped strings and no plaintext credentials**. See [docs/DOCKER.md](docs/DOCKER.md).
**The console now wears the Hermes design system**; read-only status (progress / concurrency / time left) lives in the top bar,
actions (start/stop/restart + settings/records/kb) in the sidebar footer;
the settings drawer splits into **basic**(platform / model / project)and **advanced**(run knobs).
**The image is trimmed for scoring**: it carries only the `zhongce` / `ctf` / `pentest` profiles — the fraud-investigation line (web console, AI result grading, Excel target slicing, and the `shezha` prompt/profile/knowledge files) is excluded. Local use (`./start.sh` + the 8799 console) is unaffected.


(no shadows / no radii / 4px grid / HarmonyOS Sans + Cascadia,
neutral dark & light themes); only styling and static assets changed — every class name, endpoint and piece of logic
is untouched (the pre-skin copy stays local and is not committed).
**The blackboard keeps itself clean**: everything is normalised to UTF-8 (GBK that slips in is transcoded and the
agent is told to stop doing it), and after a container is re-created the top of the blackboard lists the *current*
target plus any **dead addresses**, so agents never keep probing stale ones.
Details: **[docs/PROFILES.md](docs/PROFILES.md)**.
Pentest deliverables are `FINDINGS.md` / `findings.jsonl`; run `python3 tools/pentest_report.py`
to produce `REPORT.md`.

## What differs from the original CTF-only Rotom

| | Original Rotom (CTF) | Current main branch (Rotom_beta) |
|---|---|---|
| Scope | CTF contests only | CTF + TSec / crowd-test + pentest + fraud-site recon (five profiles) |
| API | 3 GET endpoints | TSec 5 endpoints with `BENCHMARK_TOKEN` header (CTF adapter still available) |
| Containers | always on, address given | In TSec mode: **you start/close them; max 3 active at a time** |
| Flags | one per challenge | In TSec mode: `flag_count` may be > 1 — **all flags must be captured** |
| Network | direct | In TSec mode: **targets live inside a VPN**; VPN precheck gates everything |
| Concurrency | 36 (on-site quota) | 4 (3 containers × 1–2 agents) |

## Quick start

```bash
cp .env.example .env      # a .env is already generated; fill in two values
./start.sh                # VPN precheck → platform check → dashboard → run in background
```

Fill in `BENCHMARK_BASE_URL` and `BENCHMARK_TOKEN` (issued when the benchmark task is created).
Everything else can be edited in the web console at <http://127.0.0.1:8799>.

## VPN (the only prerequisite)

Target addresses are only reachable inside the benchmark VPN, so the harness treats the VPN
precheck as a hard gate. `tools/vpn.sh up/down/status` uses `.env`'s `VPN_OVPN_FILE`;
no `openvpn` locally? `brew install openvpn`, or open the issued `.ovpn` with
Tunnelblick / OpenVPN Connect. `python3 tools/netcheck.py` does a full health check.
Note: a full-tunnel VPN also carries your model API traffic — if the model endpoint becomes
unreachable, keep `NET_PROXY=direct` and check VPN split routing.

## How it runs

Each loop: VPN precheck → rotate → container scheduling → spawn agents → launch `pi` → reap.

- **3 container slots.** `manage_containers` keeps containers open only for the three most
  deserving challenges (never-tried first, then lower level, then ones with progress).
- **Time-slice rotation.** A challenge holding a slot for `QUESTION_SLICE_SECONDS` (30 min)
  with **no new flag** yields its slot; any progress renews the lease.
- **Multi-flag.** After each successful submit the tool prints `本题进度 x/y`; the agent is
  instructed to keep hunting a *different* vulnerability until the challenge is complete.
- **Session lifetime protection** (`ADOPT_WORKERS` / `ROTATE_WAIT_RUNNING` / `WORKER_MIN_LIFETIME`,
  all on by default): on runner restart, adopt in-flight `pi` processes; never rotate a
  challenge that has a running agent; new agents get a grace period. Fixes the "38% died on
  restart" issue found in 234 measured sessions.
- **Session handoff, background jobs, anti-loop, gateway monitoring** (2026-10-26, aligned with top-ranked agents): each attempt is sliced into `SESSION_SLICE_SECONDS` (default 1200s) sessions that hand off **without burning a failed attempt**; long scans go through `tools/job.py start/status/list/stop` (per-challenge isolation); the prompt enforces "two fruitless experiments -> change surface, never re-run as-is"; model latency is sampled at start and periodically, alerting when >2x baseline (and >=5s).
- **Hints are the agent's own call** (2026-10-26): the harness no longer auto-pulls them by default — workers fetch one themselves via `python3 tools/get_hint.py <qid>` (each question costs 10% of its score once viewed); the prompt spells out the tool, the cost, and when it's worth it, with a strong recommendation from the 2nd attempt on. Re-enable auto-injection with `HINT_AFTER_ATTEMPTS>=1` (optionally `HINT_EARLY_DIFFS=hard,medium`).
- **Failures must be loud** (2026-10-25): continuous platform-API failures are re-logged at least every 10 min, container start/close failures always get a log line + board fact, and a dispatch stall (no new worker/solve for 15 min with concurrency ≤1) raises an alarm; flag backfill retries independently of the question sync; `跑一轮.sh` auto-starts the host-side watchdog `tools/round_watch.sh` (macOS notifications, no app required).
- **Stop = release.** `./start.sh stop` kills all agents and closes every container.

## Key configuration (.env; web console equivalent)

| Variable | Default | Meaning |
|---|---|---|
| `PROFILE` | `zhongce` | which profile (also selectable in the web console) |
| `MAX_ACTIVE_CHALLENGES` | 3 | simultaneous containers (platform hard cap) |
| `QUESTION_SLICE_SECONDS` | 1800 | per-challenge slot lease; 0 = no rotation |
| `START_WORKERS` / `MIN_WORKERS` | 4 / 1 | start concurrency / floor |
| `WORKER_TIMEOUT` / `MAX_WORKER_TIMEOUT` | 900 / 1800 | per-question timeout, escalates on failure |
| `ROUND_WINDOW_MINUTES` | 0 | total window (minutes); 0 = unlimited |
| `HINT_AFTER_ATTEMPTS` | 0 | auto-pull by harness after N failures (0 = default, agent decides via get_hint.py) |
| `HINT_EARLY_DIFFS` | (empty) | in auto-injection mode, tiers that get it on the first attempt |
| `HINT_SKIP_DIFFS` | easy | in auto-injection mode, tiers that never auto-pull |
| `API_FAIL_RELOG_S` | 600 | re-log continuous platform-API failures at least every N seconds |
| `STALL_WARN_S` | 900 | dispatch-stall alarm threshold (seconds; 0 = off) |
| `STALL_MIN_PENDING` | 10 | don't call it a stall when fewer questions remain |
| `ADOPT_WORKERS` / `ROTATE_WAIT_RUNNING` / `WORKER_MIN_LIFETIME` | 1 / 1 / 120 | session-lifetime protection |
| `NET_PROXY` | direct | direct connect (ignore system proxy). VPN is a tunnel, not a proxy |
| `PI_BASE_URL` / `PI_MODEL` / `PI_API_KEY` | local DeepSeek gateway | runtime override of the model endpoint: `跑一轮.sh` forwards them and `platform_env.py` rewrites pi's `models.json` at startup — **switching gateways needs no image rebuild** |
| `REDUCE_COOLDOWN` / `RECOVER_AFTER` | 60 / 1800 | cooldown between concurrency drops / how long a quiet period must last before concurrency goes back up (0 = never) |

## Monitoring / shutdown

```bash
tail -f logs/runner.out                  # scheduler log (container ready / rotate / submit / retry)
tail -f logs/submissions.log             # every submit and platform response
./start.sh status                        # one-screen status
./start.sh stop                          # stop (collect agents + close containers)
python3 tools/status_now.py              # finer-grained realtime status
python3 tools/postmortem.py              # post-mortem after a run
python3 tools/export_trace.py            # audit trail (Thought/Action/Observation)
```

## Offline self-test (no VPN, no model tokens)

```bash
python3 tests/profile_test.py            # assertions: profiles + golden comparison + pentest chain
python3 tests/session_test.py            # session-protection knob simulation
python3 tests/tsec_test.py               # 35 assertions: adapter / container / multi-flag / submit / rotation
bash tests/run_mock_e2e.sh               # end-to-end with mock platform and fake pi: 6 challenges, 9 flags
bash tests/run_startsh_mock.sh           # acceptance test of the real ./start.sh entry point
```

## Doc map

| Doc | Contents |
|---|---|
| [README.md](README.md) | this file |
| [README.en.md](README.en.md) | English version |
| [docs/PROFILES.md](docs/PROFILES.md) | profiles in depth: fields, switching, adding a 4th, prompt templates, system prompts, session knobs, rollback |
| [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md) | whole-project file map (what each file is, how they fit together) |

## License

Same as Rotom: forks and modifications welcome; **for commercial use, reach out via the project page**.