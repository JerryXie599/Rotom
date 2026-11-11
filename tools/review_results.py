#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
review_results.py —— 成果评读器:AI 通读各站点报告,评"成果程度"(shezha 包用)。

不是贴标签:每个有材料的站点,把 00_档案.md 全文 + 报告清单 + FINDINGS 摘录喂给模型**通读**,
判定:
  deep    实质战果(shell/拖库/实际获取数据面(聊天记录·订单·库表,含量级)/定位到运营者实名)
  mid     有真实获取或可复核发现(未鉴权可读面/越权读到了东西/协议密钥还原),没到 deep
  legit   **疑似正规资产,不算战果** —— 拿到的只有能证明站点正规的材料(ICP/联网备案号·营业执照·正规主体
          ·正规域名被抢注/劫持);按铁律「见正规资产立即停手」交**人工复核是否继续打**
  surface 只做了探测/指纹/判死,没有实际获取
  none    无档案材料(纯占位骨架)
输出(增量落盘,跑一半也有结果): <ws>/成果评审.json
  {"reviews": {域名: {"degree":…, "got":"到底拿到了什么", "gap":"还能怎么更深", "by":"ai",
                      "rev":<口径版本>, "decision":"stop|go"(人工复核落槌后)}}, …}
口径版本 REV:判定会盖版本章;改口径(如新增 legit)就 +1 —— 旧版 ai 判定里的 deep/mid/legit
自动重读,surface/none 直接盖章(没有获取面,重读评不出新东西,省模型配额)。

用法: python3 tools/review_results.py --ws <涉诈>/00_总控/自动批次 [--chunk 20] [--provider …] [--model …]
零网络违规:模型调用走本机 pi 配置(模型 API,不是探测流量)。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

REV = 2      # 评审口径版本。改判档口径就 +1(见文件头);旧版判定会被本脚本自动重读
DEGREES = ("deep", "mid", "legit", "surface", "none")


def _read(p: Path, cap: int) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:cap]
    except OSError:
        return ""


def load_sites(ws: Path) -> list[dict]:
    inv = json.loads((ws / "站点总库.json").read_text(encoding="utf-8"))
    return inv.get("sites") or []


_MM_STD = ("总报告.md", "时间线.md", "思维导图源文件.md")   # 新统一结构里 报告/ 下的标准四件套(html 不算材料)


def mindmap_is_real(text: str) -> bool:
    """导图源文件是不是"有内容"的:统一结构给的是 8 分支占位骨架(每分支一行"(后续探测在此增删节点)"),
    把占位行/前端 matter 剔掉后不足 200 字 = 还没填。"""
    if not text:
        return False
    body = re.sub(r"^\s*-\s*\((?:后续探测在此增删节点|待探测)\)\s*$", "", text, flags=re.M)
    body = re.sub(r"^---.*?^---", "", body, flags=re.M | re.S)      # markmap frontmatter
    body = re.sub(r"^#.*$", "", body, flags=re.M)                    # 标题
    return len(re.sub(r"\s", "", body)) >= 200


def _material_files(arch: Path) -> list[Path]:
    """一个站点"算材料"的那些文件(与 site_material 同口径)。

    新统一结构(2026-10-07 结构整顿):报告/总报告.md(本站唯一完整报告)+ 报告/时间线.md
    + 报告/思维导图源文件.md + 00_档案.md;老布局(REPORT.md/根目录报告 md/报告目录其它 md/FINDINGS)
    全部保留兼容 —— 两种布局的文件都进指纹,新料一落盘就会被判"要重读"。"""
    files: list[Path] = []
    for leaf in ("00_档案.md", "REPORT.md", "FINDINGS.md"):
        f = arch / leaf
        if f.is_file():
            files.append(f)
    for leaf in _MM_STD:
        f = arch / "报告" / leaf
        if f.is_file():
            files.append(f)
    for d in (arch / "报告", arch / "工作区"):
        if d.is_dir():
            files += [p for p in d.glob("*.md") if p.is_file() and p.name not in _MM_STD]
    files += [p for p in arch.glob("*.md") if p.is_file()]
    return files


def material_sig(ws: Path, s: dict) -> str:
    """材料指纹:材料文件的 总大小+最大 mtime。变了 = 这站有新料,值得重评。
    比逐字节 hash 便宜得多(只 stat),几百站一次也只要几毫秒。"""
    arch = ws.parent.parent / "01_目标档案" / s["lib"] / s["name"]
    if not arch.is_dir():
        return ""
    size = 0
    newest = 0.0
    for p in _material_files(arch):
        try:
            st = p.stat()
        except OSError:
            continue
        size += st.st_size
        newest = max(newest, st.st_mtime)
    return f"{size}:{int(newest)}"


def site_material(ws: Path, s: dict) -> str:
    """收集一个站点的可读材料。

    优先级 = 新统一结构在前、老布局兜底:
      ① 报告/总报告.md(本站唯一完整报告:当前结论/数据战果/实名进展/挂账 + 详录开头)
      ② 00_档案.md(状态/定性/实名线索一页纸)
      ③ 报告/时间线.md(追加式一行一探,新→旧)
      ④ 报告/思维导图源文件.md(8 分支结构摘要;占位骨架不算)
      ⑤ 老布局:REPORT.md / 报告目录其它 md / 根目录补遗 md / FINDINGS
    """
    arch = ws.parent.parent / "01_目标档案" / s["lib"] / s["name"]
    if not arch.is_dir():
        return ""
    parts = [f"## {s['domain']}({s['name']},库:{s['lib']})"]
    body = ""
    rep_md = _read(arch / "报告" / "总报告.md", 4200)
    if rep_md:
        body += "【报告/总报告.md(本站唯一完整报告,尾部有截断)】\n" + rep_md + "\n"
    prof = _read(arch / "00_档案.md", 1600)
    if prof:
        body += "【00_档案.md】\n" + prof + "\n"
    tl = _read(arch / "报告" / "时间线.md", 1100)
    if tl:
        body += "【报告/时间线.md(新 → 旧,截断)】\n" + tl + "\n"
    mm = _read(arch / "报告" / "思维导图源文件.md", 1100)
    if mm and mindmap_is_real(mm):
        body += "【报告/思维导图源文件.md(8 分支结构)】\n" + mm + "\n"
    report_md = _read(arch / "REPORT.md", 3000)
    if report_md:
        body += "【REPORT.md】\n" + report_md + "\n"
    rep = arch / "报告"
    if rep.is_dir():
        mds = sorted((p for p in rep.glob("*.md") if p.name not in _MM_STD),
                     key=lambda p: p.stat().st_mtime)[-2:]
        for p in mds:
            body += f"【报告/{p.name}】\n" + _read(p, 1600) + "\n"
    root_mds = sorted((p for p in arch.glob("*.md") if p.name != "00_档案.md"),
                      key=lambda p: p.stat().st_mtime)[-2:]   # 旧站点报告直接放根目录的年代
    for p in root_mds:
        body += f"【{p.name}】\n" + _read(p, 1200) + "\n"
    findings = _read(arch / "工作区" / "FINDINGS.md", 1200) or _read(arch / "FINDINGS.md", 1200)
    if findings:
        body += "【FINDINGS 摘录】\n" + findings + "\n"
    if not body.strip():
        return ""
    parts.append(body)
    return "\n".join(parts)


def call_model(chunk_text: str, provider: str, model: str) -> dict:
    prompt = (
        "你在评审涉诈网站侦查项目的站点成果。下面给出若干站点的档案材料(00_档案全文/报告清单/FINDINGS 摘录)。\n"
        "**请逐站通读材料**,评「成果程度」degree:\n"
        "- deep: 实质战果——拿到 shell、拖到库、实际获取了数据面(聊天记录/订单/会员表/信封消息,注明量级)、"
        "或定位到运营者实名(手机号/身份证/收款人/真人姓名);\n"
        "- mid: 有真实获取或可复核的发现(未鉴权可读面、越权读到了东西、协议/密钥还原、后台可访问),但没到 deep;\n"
        "- legit: **疑似正规资产,不算战果** —— 拿到的只有能证明站点正规性的东西:ICP/联网备案号、营业执照、"
        "正规企业主体、知名平台官方域名、被抢注/被劫持的正规域名;命中铁律「见正规资产立即停手」,交人工复核是否继续打;\n"
        "- surface: 只做了探测/指纹/判死/停放,没有实际获取任何东西;\n"
        "- none: 没有档案材料(纯占位骨架)。\n"
        "**判档口径(重点,历史最容易评错)**:\n"
        "· 材料里【报告/总报告.md】是本站**唯一完整报告**(结构整顿后的真源,尾部截断不影响判断),以它为准;\n"
        "· 单凭备案号/主体资质**不算战果**:备案号只能说明站点可能正规 → 落 legit;只有备案信息、没看到涉诈面时,"
        "got 写「仅得 XX 备案号(主体:…),无涉诈战果」,gap 写「人工复核:是否继续打」;\n"
        "· 例外:站点本身明显涉诈、备案主体是空壳(与页面业务不符/批量注册)时,备案号按**实名线索**记,"
        "仍按实际战果评 deep/mid,got 里注明「主体疑空壳」;\n"
        "· deep 的实名定位要**运营者/团伙**的真人信息(手机号·身份证·收款人·真人姓名);只有公司名+备案号不算深。\n"
        '只输出 JSON(不要解释):{"reviews": {"域名": {"degree": "deep|mid|legit|surface|none", '
        '"got": "到底拿到了什么(≤30字,只写材料里的事实)", "gap": "还能怎么更深(≤24字,没有就空串)"}}}\n'
        "每个站点都必须有;got/gap 不得编造,材料里没有依据就照实写浅。\n\n"
        "# 站点材料\n" + chunk_text)
    last = ""
    for attempt in range(2):          # 模型偶尔吐畸形 JSON(未转义引号/截断),重试一次
        r = subprocess.run(
            ["pi", "-p", "--provider", provider, "--model", model, "--no-session",
             "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files", prompt],
            capture_output=True, text=True, timeout=420)
        m = re.search(r"\{.*\}", r.stdout or "", re.S)
        if not m:
            last = f"模型没给 JSON:{(r.stderr or r.stdout or '')[:200]}"
            continue
        raw = m.group(0)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = _salvage_json(raw)   # 截断/未转义 → 抢救能救的键值
            if data is None:
                last = f"JSON 解析失败(第 {attempt + 1} 次):{raw[:160]}"
                continue
        reviews = data.get("reviews")
        return reviews if isinstance(reviews, dict) else {}
    raise RuntimeError(last)


def _salvage_json(raw: str) -> dict | None:
    """模型输出畸形 JSON 时尽量抢救:逐个抓 "域名": {...} 对,丢掉坏的。
    返回 {"reviews": {...}} 或 None。"""
    out: dict[str, dict] = {}
    # 每个 "xxx": { ... "degree": "..." ... } 块
    for m in re.finditer(r'"([^"]+)"\s*:\s*\{', raw):
        dom = m.group(1)
        start = m.end() - 1
        depth = 0
        end = None
        for i in range(start, len(raw)):
            if raw[i] == "{":
                depth += 1
            elif raw[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            continue
        block = raw[start:end]
        deg = re.search(r'"degree"\s*:\s*"(deep|mid|legit|surface|none)"', block)
        if not deg:
            continue

        def _f(key: str) -> str:
            mm = re.search(rf'"{key}"\s*:\s*"([^"]*)"', block)
            return mm.group(1) if mm else ""
        out[dom] = {"degree": deg.group(1), "got": _f("got")[:60], "gap": _f("gap")[:50]}
    return {"reviews": out} if out else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", required=True, help="涉诈/00_总控/自动批次 目录")
    ap.add_argument("--chunk", type=int, default=20, help="每次模型调用读几个站")
    ap.add_argument("--provider", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--count", action="store_true",
                    help="只报待办数量(零模型调用),自动同步用它决定该不该烧 token")
    ap.add_argument("--retry-fallback", action="store_true",
                    help="连上次被内容安全过滤(1301)的 fallback 站一起重读;默认不重试(自动同步走默认)")
    args = ap.parse_args()
    ws = Path(args.ws).expanduser()
    provider = args.provider or "bigmodel-shezha"
    model = args.model or "GLM-5.3-Flash"

    sites = load_sites(ws)
    if not sites:
        print("站点总库为空(先在控制台刷新站点总库)")
        return 2
    out_path = ws / "成果评审.json"
    try:
        reviews = (json.loads(out_path.read_text(encoding="utf-8")).get("reviews")) or {}
    except (OSError, ValueError):
        reviews = {}

    # 一个域名可能对应多个档案夹(同一站的不同面,如 uhccjcy.com 有 3 个夹),
    # 但评审是**按域名**一份的 → 先按域名归并,材料/sig 把各夹拼起来,避免重复读。
    by_dom: dict[str, list[dict]] = {}
    for s in sites:
        by_dom.setdefault(s["domain"], []).append(s)

    # 跳过条件 = 上次是 AI 读的 且 材料指纹没变 且 口径版本一致。
    #   · 有 sig 且相同 且 rev 一致 → 跳过;
    #   · 有 sig 但不同 → 材料更新过,重读;
    #   · rev 不符(口径升级)→ deep/mid/legit 重读(可能把备案号当成了战果),surface/none 直接盖章;
    #   · 无 sig(指纹机制上线前评的)→ 只补盖当前 sig,不重读(否则一次要重烧全库)。
    todo: list[tuple[dict, str]] = []
    reread = 0
    resign = 0
    stale = 0
    for dom, group in by_dom.items():
        mat = "\n\n".join(x for x in (site_material(ws, s) for s in group) if x)
        sig = "|".join(material_sig(ws, s) for s in group)
        if len(mat) < 80:
            reviews.setdefault(dom, {"degree": "none", "got": "无档案材料(仅占位)",
                                     "gap": "", "by": "auto"})
            continue
        prev = reviews.get(dom) or {}
        by = prev.get("by")
        if by == "ai" and prev.get("rev") != REV:
            if prev.get("degree") in ("deep", "mid", "legit"):
                stale += 1
                todo.append(({"domain": dom, "name": group[0]["name"],
                              "hint": next((s.get("hint") for s in group if s.get("hint")), "probe")},
                             mat))
            else:
                prev["rev"] = REV       # 没有获取面,重读评不出新东西 → 盖章放行,省配额
                if not prev.get("sig"):
                    prev["sig"] = sig
                    resign += 1
            continue
        if by == "ai":
            if not prev.get("sig"):
                prev["sig"] = sig       # 补盖指纹,视为已读(不重烧)
                resign += 1
                continue
            if prev["sig"] == sig:
                continue                # 已通读且材料没动
            reread += 1                 # 材料更新过 → 重读
        elif by == "fallback" and not args.retry_fallback:
            continue                    # 内容安全过滤拒过的站:默认不自动重试(要重来用 --retry-fallback)
        todo.append(({"domain": dom, "name": group[0]["name"],
                      "hint": next((s.get("hint") for s in group if s.get("hint")), "probe")},
                     mat))
    print(f"共 {len(sites)} 夹/{len(by_dom)} 域名:待通读 {len(todo)}"
          f"(重读 {reread} + 新站/兜底 {len(todo)-reread-stale} + 旧口径重评 {stale}),补盖指纹 {resign},"
          f"已读且无变化跳过 {len(by_dom) - len(todo) - resign}")
    if args.count:      # 自检模式:不落盘、不调模型,只报数给自动同步决策
        print(json.dumps({"total": len(by_dom), "todo": len(todo), "reread": reread, "stale": stale},
                         ensure_ascii=False))
        return 0
    out_path.write_text(json.dumps({"reviews": reviews, "generated": time.strftime("%F %T")},
                                   ensure_ascii=False, indent=1), encoding="utf-8")

    fail = 0
    total = (len(todo) + args.chunk - 1) // args.chunk
    for i in range(0, len(todo), args.chunk):
        chunk = todo[i:i + args.chunk]
        text = "\n\n".join(m for _, m in chunk)
        try:
            got = call_model(text, provider, model)
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"[批 {i // args.chunk + 1}/{total}] 调用失败:{e}(该批站点落 hint 兜底)")
            for s, _m in chunk:
                reviews.setdefault(s["domain"],
                                   {"degree": {"result": "mid", "legit": "legit"}.get(s.get("hint"), "surface"),
                                    "got": "模型调用失败,按机械信号兜底", "gap": "", "by": "fallback"})
        else:
            for s, _m in chunk:
                dom = s["domain"]
                meta = got.get(dom) or got.get(s["name"]) or {}
                degree = meta.get("degree")
                if degree not in DEGREES:
                    degree = "surface"
                prev = reviews.get(dom) or {}
                entry = {"degree": degree,
                         "got": str(meta.get("got") or "")[:60],
                         "gap": str(meta.get("gap") or "")[:50], "by": "ai", "rev": REV,
                         "sig": "|".join(material_sig(ws, g) for g in by_dom[dom])}
                if prev.get("decision"):    # 人工复核落槌过(stop/go)→ 重读保留人话,不因新料翻案
                    entry["decision"] = prev["decision"]
                    entry["decided"] = prev.get("decided")
                reviews[dom] = entry
            n_deep_c = sum(1 for s2, _ in chunk if reviews[s2["domain"]]["degree"] == "deep")
            n_legit_c = sum(1 for s2, _ in chunk if reviews[s2["domain"]]["degree"] == "legit")
            print(f"[批 {i // args.chunk + 1}/{total}] 读完 {len(chunk)} 站(deep {n_deep_c} / legit {n_legit_c})")
        # 每批都增量落盘:跑一半树上也已经能上色
        out_path.write_text(json.dumps({"reviews": reviews, "generated": time.strftime("%F %T")},
                                       ensure_ascii=False, indent=1), encoding="utf-8")
    n_deep = sum(1 for v in reviews.values() if v.get("degree") == "deep")
    n_mid = sum(1 for v in reviews.values() if v.get("degree") == "mid")
    n_legit = sum(1 for v in reviews.values() if v.get("degree") == "legit")
    print(f"完成:deep={n_deep} mid={n_mid} legit={n_legit}"
          f" surface/none={len(reviews) - n_deep - n_mid - n_legit}"
          f"(失败批 {fail});结果在 {out_path}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
