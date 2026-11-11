#!/bin/bash
# 交付物逐层审计(宿主机侧工具,不进镜像)
#
#   bash docker/audit_layers.sh                 # 审计本地 rotom-agent:latest
#   bash docker/audit_layers.sh dist/rotom-agent.tar.gz
#
# 为什么需要它:容器内 `grep -rI /app` 只能看到**最终文件系统**,看不到被后续层覆盖/删除的历史内容。
# 托管平台拿到的是 `docker save` 出来的 tar —— 里面的每个 blob(层)都得单独搜一遍,才算真干净。
# (第一版镜像就是栽在这里:"先 COPY 再覆盖一层脱敏",底层 layer 里原始文本还在。)
#
# 审计规则不在本脚本里硬编码,而是读 docker/sensitive_rules.json(本地配置,git 忽略),
# 缺省回退到 sensitive_rules.example.json;命中**定位到文件**(逐 blob 拆 tar 搜),
# 已核实的上游残留单独列出。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
TARGET="${1:-rotom-agent:latest}"
TMP=$(mktemp -d /tmp/rotom-layer-audit-XXXXXX)
trap 'rm -rf "$TMP"' EXIT

if [ -f "$TARGET" ]; then
  cp "$TARGET" "$TMP/image.tar"
  case "$TARGET" in *.gz|*.tgz) gunzip -c "$TMP/image.tar" > "$TMP/image.raw" && mv "$TMP/image.raw" "$TMP/image.tar" ;; esac
else
  echo "导出镜像 $TARGET …"
  docker save -o "$TMP/image.tar" "$TARGET" || exit 1
fi
tar -xf "$TMP/image.tar" -C "$TMP" || exit 1
python3 - "$TMP" <<'PY'
import gzip, io, json, re, subprocess, sys, tarfile
from pathlib import Path

root = Path(sys.argv[1])
# 规则来源:仓库 docker/sensitive_rules.json(本地,git 忽略)→ example 回退
for cand in (Path.cwd() / "docker" / "sensitive_rules.json",
             Path.cwd() / "docker" / "sensitive_rules.example.json"):
    if cand.exists():
        cfg = json.loads(cand.read_text(encoding="utf-8"))
        break
else:
    sys.exit("找不到 docker/sensitive_rules*.json")
cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
IDENT = "|".join(cfg["identity_words"])
EP_INTERNAL = "|".join(re.escape(e) for e in cfg["internal_endpoints"])
EP_FIELD = "|".join(re.escape(e) for e in cfg["field_endpoints"])
SHAPE_ALT = "|".join(cfg["credential_shapes"])

# 审计规则(由 sensitive_rules*.json 生成)
RULES = [
    ("宿主机绝对路径",      re.escape(cfg["host_path_prefix"])),
    ("联系方式(我们自己的)", re.escape(cfg["contact_digits"])),   # 上游包元数据里的第三方作者邮箱不算
    ("身份/话术类禁词",     IDENT),
    ("自建网关/实验网段",   EP_INTERNAL + r"[0-9]*"),
    ("VPN 客户端/现场端点", EP_FIELD + r"[0-9]*"),
    ("平台任务 ID",         r"(?<![A-Za-z0-9_])" + cfg["task_id_pattern"] + r"(?![A-Za-z0-9])"),
    # ↑ 必须带词边界:第三方二进制里到处是 C++ 符号名(…node11task_runner13…/…log_task_done…),
    #   不加边界会报几十条假阳性。
    ("明文模型 key",        r"(?<![A-Za-z0-9_])sk-(?!ecdsa|ssh-)(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{20,}"),
    # ↑ 同理:nuclei 二进制里的 elisp 符号串、SSH 算法名(sk-ecdsa-sha2-…@openssh.com /
    #   sk-ssh-ed25519-…)、WordPress 插件 slug(sk-wp-login-customization 这类,来自 nmap/nuclei 词表)都要排除 ——
    #   真 key 必然含数字,slug 全是小写单词+连字符 → 用"尾部至少一个数字"区分。
    ("自有凭据(密钥形态)",  SHAPE_ALT),
    # ↑ 自有 key 的形态(按厂商填入 sensitive_rules*.json 的 credential_shapes)。
]
# 已核实的上游残留:CPython / pip 自带的路径示例(公开上游代码,不是我们的数据)。
# 出现在这些文件里的命中单列,不计失败;其他任何文件命中都算失败。
# 注:tar 里的成员名**不带前导斜杠**。按你镜像里的实际路径增删(sensitive_rules*.json 的 upstream_ok)。
UPSTREAM_OK = set(cfg.get("upstream_ok", []))
CHUNK = 8 << 20          # 8MB;块间留 256B 重叠,防命中跨块被漏

blobs = sorted((root / "blobs" / "sha256").glob("*"))
if not blobs:
    print("没找到 blobs/ —— 这不像 docker save 的产物"); sys.exit(2)

totals: dict = {}        # (bucket, rule) -> count
files_of: dict = {}      # (bucket, rule) -> {file: count}
samples: dict = {}       # (bucket, rule) -> [str]

# 镜像 config 的 history 里会记录每条 RUN 的**命令原文** —— 我们的清理命令自带"在过滤什么"的模式串
# (如 -exec grep -lF -m1 <路径前缀> / xargs -r sed -i -E 's#<路径前缀>...#<用户目录>#g')。
# 这类命中按上下文判定为"构建命令残留"(非数据);但其他任何内容(比如 RUN 里真写了密钥)照旧算失败。
CMD_SIG = re.compile(r"-exec grep -l[FE] -m1|xargs -r sed -i -E 's#")


def add(bucket, rule, data: bytes, fname: str) -> None:
    for m in CRE[rule].finditer(data):
        b = bucket
        if b == "real" and "(非 tar)" in fname:              # config/history 场景:看上下文是不是清理命令
            s = max(0, m.start() - 130); e = m.end() + 130
            if CMD_SIG.search(data[s:e].decode("utf-8", "ignore")):
                b = "history"
        k = (b, rule)
        totals[k] = totals.get(k, 0) + 1
        d = files_of.setdefault(k, {})
        d[fname] = d.get(fname, 0) + 1
        if len(samples.setdefault(k, [])) < 3:
            s = max(0, m.start() - 90); e = m.end() + 50
            samples[k].append(fname + ": " + " ".join(data[s:e].decode("utf-8", "ignore").split())[:150])


def scan_fileobj(f, rule_names, fname, bucket) -> None:
    """按块读 + 块间重叠,避免整文件进内存。

    ⚠ 不要用"正则源码字面量 in buf"做预筛 —— 源码里带 {12,}/\\. 这类元字符,几乎永远匹配不上,
    会把规则静默废掉。真正的预筛就用编译好的正则 .search()。
    """
    global SCANNED_BYTES, SCANNED_FILES
    SCANNED_FILES += 1
    tail = b""
    while True:
        chunk = f.read(CHUNK)
        if not chunk:
            break
        SCANNED_BYTES += len(chunk)
        buf = tail + chunk
        for r in rule_names:
            if CRE[r].search(buf):
                add(bucket, r, buf, fname)
        tail = buf[-256:]
    if tail and any(CRE[r].search(tail) for r in rule_names):
        for r in rule_names:
            add(bucket, r, tail, fname)


# 扫描器自检:每条规则必须能打中自己的探针,否则直接失败(防"规则被人改坏/字符串转义写错"这类静默失效)
CRE = {n: re.compile(p.encode()) for n, p in RULES}
CRE["自建网关/实验网段"] = re.compile(EP_INTERNAL.encode())
CRE["VPN 客户端/现场端点"] = re.compile(EP_FIELD.encode())
PROBES = {
    "宿主机绝对路径": ("x " + cfg["host_path_prefix"] + "foo y").encode(),
    "联系方式(我们自己的)": cfg["contact_digits"].encode(),
    "身份/话术类禁词": ("x " + cfg["identity_words"][0] + " y").encode(),
    "自建网关/实验网段": (cfg["internal_endpoints"][0] + "5").encode(),
    "VPN 客户端/现场端点": (cfg["field_endpoints"][0] + "9").encode(),
    "平台任务 ID": ("x " + cfg["task_id_pattern"]
                     .replace("[A-Za-z0-9]{8,}", "A1b2C3d4") + " y").encode(),
    "明文模型 key": b"sk-" + b"aB3xY9qW2z" * 3,          # 形态贴近真 key(含数字)
    "自有凭据(密钥形态)": ("x " + cfg["credential_probes"][0] + " y").encode(),
}
for i, s in enumerate(cfg["credential_shapes"]):
    if not CRE["自有凭据(密钥形态)"].search(cfg["credential_probes"][i].encode()):
        print(f"!! 扫描器自检失败:自有凭据规则匹配不上探针 {i} —— 先修规则再审计"); sys.exit(3)
# 负向探针:这些**必须不匹配**(都是实测过的假阳性来源,规则一松就会误报)
NEG_PROBES = {
    "平台任务 ID": [b"_ZN4node11task_runner13ProcessRunnerE", b"_ZN11CompileTask13log_task_doneEP10"],
    "明文模型 key": [b"sk-ecdsa-sha2-nistp256-cert-v01@openssh.com", b"sk-ssh-ed25519-cert-v01@openssh.com",
                     b"makunboundmask-fieldnamestringpprint-tabprobe-file",
                     b"sk-wp-login-customization sk-multi-tag sk-post-type-access"],   # WP 插件 slug(词表)
}
for _n, _p in PROBES.items():
    if not CRE[_n].search(_p):
        print(f"!! 扫描器自检失败:{_n} 规则连探针都匹配不上 —— 先修规则再审计"); sys.exit(3)
for _n, _ps in NEG_PROBES.items():
    for _p in _ps:
        if CRE[_n].search(_p):
            print(f"!! 扫描器自检失败:{_n} 规则误报已知假阳性样本 {_p!r} —— 先收窄规则"); sys.exit(3)

SCANNED_FILES = 0
SCANNED_BYTES = 0


rule_names = [n for n, _ in RULES]
for b in blobs:
    raw = b.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        data = gzip.decompress(raw)
    elif raw[:4] == b"\x28\xb5\x2f\xfd":
        data = subprocess.run(["zstd", "-dc", str(b)], capture_output=True).stdout
    else:
        data = raw
    src = io.BytesIO(data)
    try:
        tf = tarfile.open(fileobj=src)
    except tarfile.TarError:
        tf = None
    if tf is None:                                  # 非 tar blob:整块搜(作用域=blob 本身)
        SCANNED_BYTES += len(data)
        for r in rule_names:
            if CRE[r].search(data):
                add("real", r, data, f"blob {b.name[:12]}…(非 tar)")
        continue
    for m in tf:
        if not m.isfile():
            continue
        f = tf.extractfile(m)
        if f is None:
            continue
        bucket = "upstream" if m.name in UPSTREAM_OK else "real"
        scan_fileobj(f, rule_names, m.name, bucket)

print(f"逐层审计:{len(blobs)} 个 blob,扫过 {SCANNED_FILES} 个文件 / {SCANNED_BYTES/2**30:.1f} GB(命中已定位到文件)\n")
fails = 0
print("== 交付物自身(计入门禁) ==")
for name, _ in RULES:
    c = totals.get(("real", name), 0)
    if c:
        fails += 1
    print(f"  {name:20} {'✔ 0 命中' if not c else f'✘ {c} 命中'}")
    if c:
        for fn, n in sorted(files_of[("real", name)].items(), key=lambda x: -x[1])[:5]:
            print(f"       {n:>6}  {fn}")
    for s in samples.get(("real", name), []):
        print(f"       → {s}")

up = sum(v for (b, _), v in totals.items() if b == "upstream")
hist = sum(v for (b, _), v in totals.items() if b == "history")
print(f"\n== 已知上游残留(CPython/pip 自带路径示例,不计失败):{up} 命中 ==")
for (bucket, name), c in sorted(totals.items()):
    if bucket == "upstream":
        for fn, n in files_of[(bucket, name)].items():
            print(f"  {name}: {n:>3}  {fn}")

print(f"\n== 已知构建命令残留(镜像 history 里清理命令的过滤模式串,非数据、不计失败):{hist} 命中 ==")
for (bucket, name), c in sorted(totals.items()):
    if bucket == "history":
        for fn, n in files_of[(bucket, name)].items():
            print(f"  {name}: {n:>3}  {fn}")

print()
if fails:
    print(f"结论:交付物仍有 {fails} 类命中 —— 必须在**同一层内**删除或替换")
elif hist:
    print("结论:交付物逐层零命中 ✓(仅剩上游 CPython/pip 示例 + history 里的过滤模式串,均已核实非数据)")
else:
    print("结论:交付物逐层零命中 ✓(仅剩上游 CPython/pip 自带的路径示例,已核实非我方数据)")
sys.exit(1 if fails else 0)
PY
