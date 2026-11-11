#!/bin/bash
# 镜像内脱敏(构建期执行,`docker/Dockerfile` 在 COPY 之后调用)
#
# 为什么:托管运行环境会**审计并部分公开**容器内的数据。所以与"跑起来"无关、但属于
# 本地/个人/机构身份的信息,一律不进镜像:
#   ① 身份/授权话术类禁词(机构口径,见 docker/sensitive_rules*.json 的 identity_words);
#   ② 联系方式(邮箱 / QQ / 手机号);
#   ③ 宿主机绝对路径;
#   ④ 自己的内网网关、实验网段、现场端点地址;
#   ⑤ 跨轮战果目录(kb_extra)—— 平台明令:不得内置针对题目的历史答题记忆。
#
# 规则不硬编码在本脚本里,而是读 docker/sensitive_rules.json(本地配置,git 忽略),
# 缺省回退到同目录的 sensitive_rules.example.json(示例值,结构一致)。
# 规则文本只存在于构建第一阶段,不进最终镜像(见 .dockerignore / Dockerfile)。
#
# 每条改动都会打印出来(便于审计);`docker/selfcheck.sh` 会再断言一遍,漏了就构建失败。
# 幂等:重复执行不会累积改动。
# 用法:
#   bash docker/sanitize.sh            # 默认清 /app(镜像构建阶段用)
#   bash docker/sanitize.sh <目录>      # 清指定目录(宿主机的仓库、构建暂存目录都能用)
set -uo pipefail
ROOT_ARG="${1:-/app}"
cd "$ROOT_ARG" || exit 1
SANITIZE_ROOT="$ROOT_ARG" python3 - <<'PY'
import json
import os
import re
from pathlib import Path

ROOT = Path(os.environ.get("SANITIZE_ROOT") or "/app")
RULES_PATH = ROOT / "docker" / "sensitive_rules.json"
if not RULES_PATH.exists():
    RULES_PATH = ROOT / "docker" / "sensitive_rules.example.json"
cfg = json.loads(RULES_PATH.read_text(encoding="utf-8"))
cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
print(f"[sanitize] 规则来源:{RULES_PATH.relative_to(ROOT)}")

IDENT = "|".join(cfg["identity_words"])
changed: list[str] = []


def rewrite(path: Path, pairs: list[tuple[str, str]], regexes: list[tuple[str, str]] = ()) -> None:
    """就地重写文件:先做精确替换,再做正则替换;有变化才写回并记账。"""
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return
    original = text
    for a, b in pairs:
        text = text.replace(a, b)
    for pat, rep in regexes:
        text = re.sub(pat, rep, text)
    if text != original:
        path.write_text(text, encoding="utf-8")
        changed.append(str(path.relative_to(ROOT)))


TEXT_GLOBS = ("**/*.md", "**/*.py", "**/*.json", "**/*.sh", "**/*.txt")
# 只处理"要发布的那套文件":运行数据/测试/第三方公开资料/本脚本自己都不动
# (镜像构建时 .dockerignore 已经把这些挡在上下文之外;这条排除表让"在仓库上直接跑"也安全)
skip_dirs = ("knowledge/vendor", "docker", "logs", "work", "projects", "traces", "dist",
             "backup", "tests", "__pycache__", ".git")
skip_suffix = (".bak", ".pyc", ".command", ".png", ".zip", ".gz", ".tar")
skip_names = {"board.json", "board.lock", "rotom-attack-graph.html", ".env"}


def files():
    for g in TEXT_GLOBS:
        for p in ROOT.glob(g):
            rel = str(p.relative_to(ROOT))
            if any(rel == d or rel.startswith(d + "/") for d in skip_dirs):
                continue
            if p.name in skip_names or p.name.startswith(".env") or p.suffix in skip_suffix or "bak" in p.name:
                continue
            if p.is_file():
                yield p


# 全库:宿主机路径、自建/实验地址、现场端点、裸邮箱与手机号、凭据形态、平台任务 ID
EP_INTERNAL = "|".join(re.escape(e) for e in cfg["internal_endpoints"])
EP_FIELD = "|".join(re.escape(e) for e in cfg["field_endpoints"])
REGEXES = [
    (re.escape(cfg["host_path_prefix"]) + r"[A-Za-z0-9._-]+(?:/[^\s`\"')）】,，;；]*)?", "<工作目录>"),
    (rf"\b(?:{EP_INTERNAL})[0-9]+(?::[0-9]+)?", "<内部地址>"),               # 自己的网关/实验网段
    (rf"\b(?:{EP_FIELD})[0-9]+", "<现场端点>"),
    (rf"\b{cfg['task_id_pattern']}\b", "<任务ID>"),
    (rf"[A-Za-z0-9._%+-]+@(?:{cfg['contact_email_domains']})\.[A-Za-z]{{2,}}", "<联系方式>"),
    (rf"(?<![0-9]){cfg['phone_pattern']}(?![0-9])", "<手机号>"),
    # 凭据形态:万一哪个配置文件(如 providers.json)混进镜像,把密钥就地打码
] + [(s, "<凭据>") for s in cfg["credential_shapes"]]
for p in files():
    rewrite(p, [], REGEXES)

# 第三方公开模板里的"示例串"(nuclei 官方模板):
#    它们不是我们的信息(上游公开内容),但交付物里不出现更省事,而且都只是示例值,改掉不影响模板功能。
#    同时删掉 nuclei 生成的两个索引缓存(可重建;里面含模板全文副本,是命中大户)。
NT = ROOT / "knowledge/vendor/nuclei-templates"
if NT.exists():
    for cache in (NT / ".templates-index", NT / ".checksum"):
        if cache.exists():
            cache.unlink()
            changed.append(f"{cache.relative_to(ROOT)}(已删除:可重建缓存)")
    TEMPLATE_PATTERNS = [
        (re.escape(cfg["host_path_prefix"]) + r"[A-Za-z0-9._-]+", "<用户目录>"),
        (r"\b192\.168\.[0-9]+\.[0-9]+\b", "<示例内网IP>"),
        (IDENT, "<敏感词>"),
        (rf"[A-Za-z0-9._%+-]+@(?:{cfg['contact_email_domains']})\.[A-Za-z]{{2,}}", "<示例账号>"),
    ]
    touched = 0
    # 注意:不止 yaml —— 模板树里还有 cves.json / *.md / *.txt 这类文本索引(实测残留就在那儿)
    for y in [q for q in NT.rglob("*") if q.is_file() and q.suffix.lower() in (".yaml", ".yml", ".json", ".md", ".txt", ".csv")]:
        try:
            t = y.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        n = t
        for pat, rep in TEMPLATE_PATTERNS:
            n = re.sub(pat, rep, n)
        if n != t:
            y.write_text(n, encoding="utf-8")
            touched += 1
    if touched:
        changed.append(f"knowledge/vendor/nuclei-templates/*.yaml({touched} 个模板的示例串已占位化)")

# kb_extra(跨轮战果)必须为空 —— 平台规则:不得内置针对题目的历史答题记忆
prof = ROOT / "profiles/zhongce.json"
if prof.exists():
    d = json.loads(prof.read_text(encoding="utf-8"))
    if d.get("kb_extra"):
        d["kb_extra"] = []
        prof.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        changed.append("profiles/zhongce.json(kb_extra 已清空)")

print(f"[sanitize] 改动 {len(changed)} 个文件:" if changed else "[sanitize] 无需改动 ✓")
for c in sorted(set(changed)):
    print(f"  - {c}")

# 自查:替换后不应再命中这些模式(自我验证,错了立刻可见)
BAD = [IDENT, re.escape(cfg["host_path_prefix"]),
       "|".join(re.escape(e) for e in cfg["internal_endpoints"]),
       rf"[A-Za-z0-9._%+-]+@(?:{cfg['contact_email_domains']})\."]
hits = []
for p in files():
    try:
        t = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    for b in BAD:
        if re.search(b, t):
            hits.append(f"{p.relative_to(ROOT)}: {b}")
print("[sanitize] 残留检查:", "干净 ✓" if not hits else f"命中 {len(hits)} 处")
for h in hits[:8]:
    print("  !", h)
if hits:
    # 硬失败:脱敏规则只在"构建的第一阶段"执行 —— 规则文本本身不进最终镜像,
    # 所以这里必须把住关,否则交付物里就会带着这些串。
    import sys
    print("[sanitize] 交付物不干净 → 构建失败")
    sys.exit(1)
PY
