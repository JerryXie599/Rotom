#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
excel_targets.py —— 目标榜单 Excel → 本批 targets.json(Rotom 通用工具,shezha/涉诈批次用)。

机制进 Agent 仓库,项目策略进 --config(一份 JSON,放项目自己的数据目录里):
  python3 tools/excel_targets.py --config <项目>/config.json <Excel1.xlsx> [...] [--批次大小 10] ...

config 字段(全部可选,root 例外可由 config 位置推出):
  root               作战仓库根(缺省 = config.json 上两级,<root>/00_总控/自动批次/config.json)
  archive_dir        档案总库目录名(相对 root,默认 01_目标档案)
  default_lib        新档默认来源库(默认 其他来源_未入四表)
  stop_set           点名停打键列表(默认 [])
  archive            归档结案表 {键: {at, reason}}(控制台"归档"写它;有归档 = 已结案,不再派发)
  stop_hints         档案文件夹名含此类词 → 停打(默认 ["不予探测"])
  category           目标大类(默认 涉诈;走策略包 playbooks 映射)
  engagement_prefix  targets.json engagement 前缀(默认 涉诈网站侦查)
  egress_note        追加进每条 notes 的出口说明
  lane_note          LANE_CLAIM 的"车道"行文案

做什么(与旧 excel转targets.py 一致):
  1. 扫 Excel 全部 sheet 逐行提取 域名/IP/URL(中文混合域名兼容);
  2. --inspect:只输出结构探测 JSON(表头/样例/每列域名密度),给自动识别与 Agent 认列当输入;
  3. --apply-map:按(Agent)列映射读列;
  4. 排除:stop_set/停打档/已建档(--redo 强制)/已派发台账;
  5. 切片建档骨架(证据/报告/大件存证/工作区),生成 targets.json(workdir 直指 工作区/),
     写 LANE_CLAIM,追加已派发台账。零网络请求。
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

URL_TOKEN = re.compile(
    r'\b(?:[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?\.)+[a-z]{2,20}\b', re.I)
IP_TOKEN = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
TLD_RE = re.compile(r'\.[a-z]{2,20}$', re.I)
FILE_EXT_RE = re.compile(
    r'\.(?:html?|php\d?|aspx?|jsp|js|css|png|jpe?g|gif|webp|svg|ico|pdf|docx?|xlsx?|pptx?|zip|rar|7z|gz|tgz|apk|ipa|exe|dmg|msi|csv|json|xml|txt|log|sql|db|sqlite|mp4|mp3|m3u8|ts|woff2?|ttf|so|dll|bin|dat)$', re.I)
URL_FULL_RE = re.compile(r'https?://[^\s\'"<>）(,，;；]+', re.I)


class Cfg:
    """--config 的承载:字段缺省全部内置,root 可由 config 文件位置推出。"""

    def __init__(self, cfg_path: Path):
        raw = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        self.dir = cfg_path.resolve().parent
        root = raw.get("root")
        self.root = Path(root).expanduser() if root else self.dir.parent.parent
        self.archive = self.root / (raw.get("archive_dir") or "01_目标档案")
        self.default_lib = raw.get("default_lib") or "其他来源_未入四表"
        self.stop_set = set(raw.get("stop_set") or [])
        # 归档结案(控制台写):键=域名/IP,值={at, reason}。归档 = 人已经有结论了(不是涉诈/已交付),
        # 与 stop_set 一样是硬排除 —— --redo 也翻不了(要重打先去控制台撤销归档)。
        arc = raw.get("archive") or {}
        self.archive_set = set(arc.keys() if isinstance(arc, dict) else arc)
        self.stop_hints = list(raw.get("stop_hints") or ["不予探测"])
        self.category = raw.get("category") or "涉诈"
        self.engagement_prefix = raw.get("engagement_prefix") or "涉诈网站侦查"
        self.egress_note = raw.get("egress_note") or ""
        self.lane_note = raw.get("lane_note") or "Rotom 全自动批次"
        self.ledger = self.dir / "已派发台账.jsonl"
        self.targets_out = self.dir / "targets.json"

    def lane_path(self, batch: int) -> Path:
        return self.root / f"LANE_CLAIM_批次{batch}.md"


def norm_key(tok: str) -> str | None:
    tok = tok.strip().lower().rstrip('/').rstrip('.')
    if IP_TOKEN.fullmatch(tok):
        return tok
    if tok and ' ' not in tok and TLD_RE.search(tok) and not FILE_EXT_RE.search(tok):
        return tok
    return None


def _host_is_routable(host: str) -> bool:
    """公网 IP 才算目标;私网/回环/链路本地是榜单噪声。"""
    try:
        import ipaddress
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or
                    ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        return True   # 域名交给 TLD/扩展名规则判


def _key_from_host(host: str) -> str | None:
    """host(可能含端口/中文)→ 目标键。中文混合域名是涉诈常态,保留原样(小写)。"""
    host = host.strip().strip('[]').split('/')[0].split('?')[0].split('#')[0]
    host = host.rsplit(':', 1)[0] if not host.startswith('[') else host
    host = host.strip('.').lower()
    if not host or ' ' in host:
        return None
    if IP_TOKEN.fullmatch(host):
        return host if _host_is_routable(host) else None
    if URL_TOKEN.fullmatch(host):
        return norm_key(host)
    # 含中文/下划线等混合形态:要求形如 x.y(至少一个点),剥文件名后缀
    if '.' in host and not FILE_EXT_RE.search(host) and '/' not in host:
        return host
    return None


def extract_tokens(text: str) -> list[str]:
    """一行文本里的候选目标:完整 URL 的 host 优先(兼容中文域名),再补裸域名/IP。"""
    out: list[str] = []
    seen: set[str] = set()

    def add(k: str | None):
        if k and k not in seen:
            seen.add(k)
            out.append(k)

    for m in URL_FULL_RE.finditer(text):
        raw = m.group(0)[len('http://'):] if m.group(0).lower().startswith('http://') \
            else m.group(0)[len('https://'):]
        add(_key_from_host(raw))
    for m in URL_TOKEN.finditer(text):
        add(norm_key(m.group(0)))
    for m in IP_TOKEN.finditer(text):
        if _host_is_routable(m.group(0)):
            add(m.group(0))
    return out


def stop_keys(cfg: Cfg) -> set[str]:
    """硬排除键:点名停打 + 归档结案 + "不予探测"类停打档。"""
    keys = set(cfg.stop_set) | set(cfg.archive_set)
    if cfg.archive.is_dir():
        for hint in cfg.stop_hints:
            for p in cfg.archive.rglob(f"*{hint}*"):
                keys.add(norm_key(p.name.split('_')[0]) or "")
    keys.discard("")
    return keys


def archived_keys(cfg: Cfg) -> dict[str, str]:
    out: dict[str, str] = {}
    if not cfg.archive.is_dir():
        return out
    for lib in cfg.archive.iterdir():
        if not lib.is_dir() or lib.name.startswith('.') or lib.name.startswith('00_'):
            continue
        for folder in lib.iterdir():
            if not folder.is_dir():
                continue
            k = norm_key(folder.name.split('_')[0])
            if k:
                out.setdefault(k, str(folder.relative_to(cfg.root)))
    skip = {"00_总控", "01_目标档案", "02_批次报告", "武器库", "自动批次"}
    for folder in cfg.root.iterdir():
        if folder.is_dir() and folder.name not in skip and not folder.name.startswith(('.', 'LANE_CLAIM')):
            k = norm_key(folder.name.split('_')[0])
            if k:
                out.setdefault(k, folder.name)
    return out


def ledger_keys(cfg: Cfg) -> set[str]:
    if not cfg.ledger.exists():
        return set()
    out = set()
    for line in cfg.ledger.read_text(encoding="utf-8").splitlines():
        try:
            out.add(json.loads(line).get("key") or "")
        except json.JSONDecodeError:
            continue
    out.discard("")
    return out


def find_next_batch(cfg: Cfg) -> int:
    prefix = "LANE_CLAIM_批次"
    nums = [0]
    for p in cfg.root.glob(f"{prefix}*.md"):
        m = re.search(r'批次(\d+)', p.name)
        if m:
            nums.append(int(m.group(1)))
    report_dir = cfg.root / "02_批次报告"
    if report_dir.is_dir():
        for p in report_dir.rglob("本批汇总_*批次*.md"):
            m = re.search(r'批次(?:百([一二三四五六七八九十]+))?(\d+)?', p.name)
            if m and m.group(2):
                nums.append(int(m.group(2)))
    return max(nums) + 1


CN = "一二三四五六七八九十"


def cn_num(n: int) -> str:
    if n < 10:
        return CN[n - 1]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + CN[n - 11]
    if n < 100:
        return CN[n // 10 - 1] + "十" + (CN[n % 10 - 1] if n % 10 else "")
    return str(n)


def lib_for(cfg: Cfg, p: Path, default_lib: str) -> str:
    """每个榜单各归各的来源库:<archive>/<文件名同名库> 存在就用它,否则 default_lib。

    上传副本常带时间戳前缀(20261003_141537_网站榜单.xlsx),剥掉再比对。
    """
    stem = re.sub(r"^\d{8}_\d{6}_", "", p.stem)
    if (cfg.archive / stem).is_dir():
        return stem
    return default_lib


def read_excel(path: Path, sheet_map: dict | None = None) -> list[tuple[str, str, int, str, str]]:
    """返回 [(key, 原始片段, sheet名, 行号, 备注)]。

    sheet_map 给定时(Agent 认列的结果):只读 include 的 sheet,目标取 url_cols 列,
    note_cols 列并进备注;不给定就走逐行全文本提取(自动识别口径)。
    """
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    found: list[tuple[str, str, int, str, str]] = []
    seen: set[str] = set()
    for ws in wb.worksheets:
        sm = None
        if sheet_map is not None:
            # key 兼容三种形状:"<原文件名>|<sheet>"(Agent 认列口径)/ "<stem>|<sheet>" / 裸 sheet 名
            sm = (sheet_map.get(f"{path.name}|{ws.title}")
                  or sheet_map.get(f"{path.stem}|{ws.title}")
                  or sheet_map.get(ws.title))
        if sheet_map is not None and not (sm and sm.get("include")):
            continue
        url_cols = set(sm.get("url_cols") or []) if sm else None
        note_cols = sorted(set(sm.get("note_cols") or []) if sm else set())
        for rowno, row in enumerate(ws.iter_rows(), start=1):
            # read_only 模式的空位是 EmptyCell 对象(不是 None),必须判 .value
            vals = [("" if c.value is None else str(c.value).strip()) for c in row]
            if not any(vals):
                continue
            note = " | ".join(v for i, v in enumerate(vals) if v and i in note_cols)[:120]
            if url_cols is not None:
                text = " ".join(v for i, v in enumerate(vals) if v and i in url_cols)
            else:
                text = " ".join(v for v in vals if v)
            for tok in extract_tokens(text):
                if tok in seen:
                    continue
                seen.add(tok)
                first = next((v for v in vals if v), "")
                found.append((tok, first[:60], ws.title, rowno, note))
    wb.close()
    return found


def inspect_excels(paths: list[str]) -> dict:
    """结构探测:每 sheet 的 key/表头/样例/每列域名密度 —— 自动识别的依据,也是 Agent 认列的输入。"""
    from openpyxl import load_workbook
    out: dict = {"files": [Path(p).name for p in paths], "sheets": []}
    for p in paths:
        try:
            wb = load_workbook(p, read_only=True, data_only=True)
        except Exception as e:
            out["sheets"].append({"file": Path(p).name, "sheet": f"(读不开: {e})"})
            continue
        for ws in wb.worksheets:
            grid: list[list[str]] = []
            col_tokens: list[int] = []
            ncols = 0
            for rowno, row in enumerate(ws.iter_rows(max_row=500), start=1):
                vals = [("" if c.value is None else str(c.value).strip()) for c in row]
                ncols = max(ncols, len(vals))
                if rowno <= 8:
                    grid.append(vals)
                if rowno > 1:                       # 首行按表头对待,不参与密度
                    while len(col_tokens) < len(vals):
                        col_tokens.append(0)
                    for i, v in enumerate(vals):
                        if v and extract_tokens(v):
                            col_tokens[i] += 1
            header = next((g for g in grid if sum(1 for v in g if v) >= max(2, ncols // 2)), [])
            out["sheets"].append({
                "key": f"{Path(p).name}|{ws.title}",
                "file": Path(p).name, "sheet": ws.title,
                "rows_scanned": min(ws.max_row or 0, 500), "cols": ncols,
                "header": [v for v in header if v][:20],
                "sample": [[v for v in g[:12]] for g in grid[1:4]],
                "col_tokens": col_tokens[:20],
            })
        wb.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="目标榜单 Excel → 本批 targets.json(通用引擎)")
    ap.add_argument("--config", required=True, help="项目策略 JSON(见文件头注释)")
    ap.add_argument("excels", nargs="*", help="榜单/清单 Excel(可多份)")
    ap.add_argument("--批次号", type=int, default=0, help="批次号(缺省=历史最大+1)")
    ap.add_argument("--批次大小", type=int, default=10)
    ap.add_argument("--档案库", default="", help="新档默认来源库(缺省用 config;同名库优先)")
    ap.add_argument("--redo", default="", help="子串匹配已建档键,命中则允许重打(续打既有档案)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--inspect", action="store_true",
                    help="只输出各 sheet 结构探测 JSON(表头/样例/每列域名密度),不切批次")
    ap.add_argument("--apply-map", default="",
                    help='按 Agent 认列结果读列:{"Sheets":{...}} 或 {"<sheet>":{...}}')
    args = ap.parse_args()

    cfg = Cfg(Path(args.config).expanduser())
    default_lib = args.档案库 or cfg.default_lib

    if args.inspect:
        print(json.dumps(inspect_excels([str(Path(p).expanduser()) for p in args.excels]),
                         ensure_ascii=False))
        return 0
    if not args.excels:
        print("✘ 缺 Excel 参数")
        return 2
    sheet_map = None
    if args.apply_map:
        try:
            sheet_map = json.loads(args.apply_map)
        except json.JSONDecodeError as e:
            print(f"✘ --apply-map 不是合法 JSON: {e}")
            return 2
        if isinstance(sheet_map, dict) and "Sheets" in sheet_map:
            sheet_map = sheet_map["Sheets"]          # 兼容 Agent 输出的包装形状

    stop = stop_keys(cfg)
    arch = archived_keys(cfg)
    done = ledger_keys(cfg)
    redo = args.redo.strip().lower()

    batch_no = args.批次号 or find_next_batch(cfg)
    today = datetime.date.today().isoformat()

    picked: list[dict] = []
    saturated = False
    stats = {"总提取": 0, "停打": 0, "归档": 0, "已建档": 0, "历史已派发": 0, "重复": 0, "入选": 0}
    for excel in args.excels:
        p = Path(excel).expanduser()
        if not p.exists():
            print(f"✘ 文件不存在:{p}")
            return 2
        for key, raw, sheet, rowno, note in read_excel(p, sheet_map):
            stats["总提取"] += 1
            if key in stop:
                stats["归档" if key in cfg.archive_set else "停打"] += 1
                continue
            if key in arch and (not redo or redo not in key):
                stats["已建档"] += 1
                continue
            if key in done and (not redo or redo not in key):
                stats["历史已派发"] += 1
                continue
            if any(x["key"] == key for x in picked):
                stats["重复"] += 1
                continue
            if saturated:
                continue        # 切片已满,继续扫完只为统计数字准确
            stats["入选"] += 1
            src = f"{p.name}[{sheet}]第{rowno}行(原文:{raw})"
            if note:
                src += f"(备注:{note})"
            picked.append({"key": key, "address": key, "name": key, "source": src,
                           "lib": lib_for(cfg, p, default_lib)})
            if len(picked) >= args.批次大小:
                saturated = True

    print(f"批次{batch_no}({today}) 候选统计:{json.dumps(stats, ensure_ascii=False)}")
    if not picked:
        print("没有可派发的新目标(全部停打/已建档/已派发)。加 --redo 子串可强制重打既有档案。")
        return 1

    lane = cfg.lane_path(batch_no)
    targets = []
    for t in picked:
        folder = cfg.archive / t["lib"] / t["name"]
        workdir = folder / "工作区"
        if not args.dry_run:
            for sub in ("证据", "报告", "大件存证", "工作区"):
                (folder / sub).mkdir(parents=True, exist_ok=True)
        notes = (f"来源:{t['source']};批次:批次{batch_no}({today});"
                 f"档案目录:{folder};LANE_CLAIM:{lane};"
                 f"停打名单:{','.join(sorted(cfg.stop_set)) or '无'};"
                 f"归档名单:{','.join(sorted(cfg.archive_set)) or '无'}")
        if cfg.egress_note:
            notes += f";{cfg.egress_note}"
        targets.append({
            "id": t["name"], "name": t["name"], "address": t["address"],
            "category": cfg.category, "workdir": str(workdir), "notes": notes,
        })

    if args.dry_run:
        print("【dry-run】将派发:")
        for t in targets:
            print(f"  - {t['id']}  ←  {t['notes'][:60]}…")
        return 0

    cfg.targets_out.write_text(json.dumps({
        "engagement": f"{cfg.engagement_prefix} 批次{batch_no}({today})",
        "targets": targets,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lane.write_text(f"""# LANE_CLAIM_批次{cn_num(batch_no) if batch_no <= 199 else batch_no}(自动批次,{today})

- 车道:{cfg.lane_note}
- 出口:{cfg.egress_note or '见 targets.json notes(一切探测显式走代理,零本机直连)'}
- 目标清单(共 {len(targets)} 个,来源见 targets.json notes):
""" + "".join(f"  - {t['id']}\n" for t in targets) + f"""
- 撞车检查:与常驻登记表在跑目标零重叠(开工时 ps 对账);以上目标均未建档/未被历史批次派发
- 停打名单:{','.join(sorted(cfg.stop_set)) or '无'}(命中即不碰)
- 归档名单:{','.join(sorted(cfg.archive_set)) or '无'}(已结案,命中即不碰)
- 状态:进行中(收工跑 收工联动.py --批次 {batch_no} 归档)
""", encoding="utf-8")

    with cfg.ledger.open("a", encoding="utf-8") as f:
        for t in picked:
            f.write(json.dumps({"batch": batch_no, "key": t["key"], "date": today},
                               ensure_ascii=False) + "\n")

    print(f"✔ targets.json 写入 {len(targets)} 个目标 → {cfg.targets_out}")
    print(f"✔ 车道申报 → {lane}")
    print("下一步:控制台「涉诈自动批次」卡片点 ② 开批次(或 bash 开批次.sh)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
