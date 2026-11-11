#!/bin/bash
# 镜像内自检(构建时 + 进容器后都能跑):确认"能解题"且"干净"。
#
#   bash docker/selfcheck.sh --build    # 构建期:硬失败(不干净就别出镜像)
#   bash docker/selfcheck.sh            # 容器内:只报告
#
# 检查两类东西:
#  A. 能力:解题要用的工具/知识库/模型 CLI 是否都在(离线可用)
#  B. 合规:不得内置"针对题目的历史答题记忆"、答案样式、明文凭据、宿主机路径
set -uo pipefail
cd /app 2>/dev/null || cd "$(dirname "$0")/.." || exit 1
BUILD=0; [ "${1:-}" = "--build" ] && BUILD=1

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  \033[32m✔\033[0m %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31m✘\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
sec()  { printf '\n== %s ==\n' "$*"; }

sec "A. 能力:工具链(离线可用)"
for t in python3 pi node curl nmap sqlmap dirsearch gobuster ffuf masscan hydra dirb nuclei \
         gdb objdump readelf strace r2 radare2 patchelf hashcat john tshark binwalk exiftool \
         sqlite3 redis-cli mysql java javac php gcc make jq rg timeout; do
  if command -v "$t" >/dev/null 2>&1; then ok "$t → $(command -v "$t")"; else bad "缺工具:$t"; fi
done
for t in nikto; do
  if command -v "$t" >/dev/null 2>&1; then ok "$t → $(command -v "$t")"; else warn "缺工具:$t(构建期下载失败;不影响主流程)"; fi
done
python3 - <<'PY' && ok "python 库(requests/pwntools/pycryptodome/z3/sympy)可导入" || bad "python 库缺失"
import importlib
for m in ("requests", "pwn", "Crypto", "z3", "sympy"):
    importlib.import_module(m)
PY
[ -d /root/nuclei-templates ] && [ "$(find /root/nuclei-templates -name '*.yaml' | wc -l | tr -d ' ')" -gt 100 ] \
  && ok "nuclei 模板已预置($(find /root/nuclei-templates -name '*.yaml' | wc -l | tr -d ' ') 个)" \
  || warn "nuclei 模板不完整(离线跑 nuclei 可能没模板可用)"
[ -f /usr/local/share/tools/jdwp-shellifier.py ] && ok "jdwp-shellifier 已预置" || warn "jdwp-shellifier 未预置"
[ -f /usr/local/share/tools/ysoserial-all.jar ] && ok "ysoserial 已预置" || warn "ysoserial 未预置"
[ -d /usr/share/wordlists ] && ok "字典目录存在($(ls /usr/share/wordlists | head -3 | tr '\n' ' '))" || warn "无字典目录"
command -v orb >/dev/null && ok "orb 兼容层已就位(macOS 老写法可直接执行)" || bad "缺 orb 兼容层"
python3 tools/kb.py search "sql 注入" >/dev/null 2>&1 && ok "知识库可检索(kb.py search)" || bad "知识库索引异常"
python3 tools/kb.py hit "8188" >/dev/null 2>&1 && ok "指纹命中可用(kb.py hit)" || bad "kb.py hit 异常"

# 凭据作业装备(厂商字典 / 产品词×年份生成器 / 组表器)—— 跑分里"凭据墙"卡点的主力
WL=knowledge/wordlists
if [ -d "$WL/vendor-passwords" ]; then
  nv=$(ls "$WL/vendor-passwords"/*.txt 2>/dev/null | wc -l | tr -d ' ')
  np=$(cat "$WL"/vendor-passwords/*.txt 2>/dev/null | grep -vcE '^\s*(#|$)')
  ok "厂商定向口令表已预置(${nv} 个表 / ${np} 条)"
else
  bad "缺 $WL/vendor-passwords/(厂商定向口令表)"
fi
[ -s "$WL/vendor-vocab.txt" ] && [ -s "$WL/usernames.txt" ] \
  && ok "厂商词根表与用户名面已预置" || bad "缺 vendor-vocab.txt / usernames.txt"
python3 tools/gen_product_year.py --vendor 泛微 --compact 2>/dev/null | grep -qx "Weaver@2023" \
  && ok "产品词×年份生成器可用(gen_product_year.py)" || bad "gen_product_year.py 异常"
python3 tools/dict_prep.py --list >/dev/null 2>&1 && ok "凭据组表器可用(dict_prep.py)" || bad "dict_prep.py 异常"
python3 tools/kb.py search "凭据墙" >/dev/null 2>&1 \
  && ok "凭据墙 playbook 已进索引(kb.py show playbooks/credential-wall)" || bad "凭据墙 playbook 未进索引"

sec "B. 合规:不得内置历史答题记忆 / 答案 / 凭据"
if python3 - <<'PY'
import json, sys
d = json.load(open("profiles/zhongce.json", encoding="utf-8"))
sys.exit(0 if not d.get("kb_extra") else 1)
PY
then ok "profiles/zhongce.json 的 kb_extra 为空(无跨轮战果目录)"; else bad "kb_extra 非空 —— 平台明令禁止内置历史答题记忆"; fi

# 跑分镜像不含涉诈(第四策略包)那条线 —— 见 .dockerignore 对应段落;混回来就报错。
# 判据分两种场合:构建期(--build,此时 .dockerignore 已生效)看到就是**错**;
# 本地仓库里当然有这些文件(只是不进镜像),所以本地只提示、不算失败。
SHZ=""
for p in dashboard.py tools/review_results.py tools/excel_targets.py prompts/shezha \
         profiles/shezha.json knowledge/shezha.md CHANGELOG.md; do
  [ -e "$p" ] && SHZ="$SHZ $p"
done
if [ -z "$SHZ" ]; then
  ok "跑分镜像不含涉诈线(控制台/成果评读/Excel 切片/涉诈策略包)"
elif [ "$BUILD" = "1" ]; then
  bad "涉诈线漏进镜像:$SHZ(见 .dockerignore)"
else
  warn "本地仓库存在涉诈件$SHZ(不进镜像,属正常;构建期会断言它们不在)"
fi
for p in profiles/zhongce.json profiles/pentest.json prompts/zhongce/prompt.md prompts/pentest/prompt.md; do
  [ -e "$p" ] || bad "跑分要用的文件缺失:$p"
done
[ -e profiles/zhongce.json ] && [ -e prompts/zhongce/prompt.md ] \
  && ok "众测(zhongce)策略包与提示词包在镜像内" || bad "zhongce 包不完整"

for p in work logs projects traces dist backup board.json kb_extra; do
  if [ -e "$p" ]; then
    if [ "$p" = "logs" ] || [ "$p" = "work" ]; then
      n=$(find "$p" -type f 2>/dev/null | wc -l | tr -d ' ')
      [ "$n" = "0" ] && ok "$p/ 为空(运行时才写入)" || warn "$p/ 里有 $n 个文件(容器运行中属正常)"
    else
      bad "不该出现在镜像里:$p"
    fi
  else
    ok "无 $p(已排除)"
  fi
done

# 答案样式扫描:uuid 型 flag / 平台题号 / 分值字面量
if grep -rIlE "flag\{[0-9a-f]{8}-[0-9a-f]{4}|FLAG\{[a-z0-9_]{10,}\}" \
     --include="*.md" --include="*.py" --include="*.json" --include="*.txt" --include="*.yaml" . 2>/dev/null \
     | grep -v "^./tests/" | head -5 | grep -q . ; then
  bad "发现 flag 样式的字符串(疑似答案残留,见上)"
else
  ok "全库无 uuid 型 flag 样式字符串"
fi
if grep -rIlE "CVE-2026-5(6670|6671|6672|6673)" --include="*.md" . 2>/dev/null | grep -v "^./knowledge/cves/" | head -3 | grep -q .; then
  warn "CVE 卡以外的地方出现了具体漏洞编号(确认是否必要)"
else
  ok "无按题号/内网地址特化的内容"
fi
# 凭据表反作弊:跨轮口令库/变形族里不许出现 flag、uuid、题号/任务 ID(那属于"答案",不是"手法")
if grep -nE "flag\{|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}|xben-[0-9]+|task_[A-Za-z0-9]{6,}" \
     knowledge/wordlists/observed-passwords.txt knowledge/wordlists/weak-leet.txt \
     knowledge/wordlists/weak-affix.txt 2>/dev/null | head -3 | grep -q . ; then
  bad "跨轮口令库里出现答案样式(flag/uuid/题号)"
else
  ok "跨轮口令库无答案样式(flag/uuid/题号)"
fi
# 说明:"身份类措辞 / 联系方式 / 宿主机路径 / 自建网段"这几类的清理由**构建第一阶段**(docker/sanitize.sh)
# 硬失败把关 —— 那一步的规则表与命中样本都不会进最终镜像(所以这里不再重复这些字面量断言)。
ok "第①类敏感措辞 / 联系方式 / 宿主机路径 / 自建网段:已在构建第一阶段硬失败把关"
# 自建/实验网段地址同理:清理由第一阶段硬失败把关(这里不重复字面量)
ok "自建/实验网段地址:已在构建第一阶段硬失败把关"
if [ -s .env ]; then
  if grep -qE "^(BENCHMARK_TOKEN|PI_API_KEY)=.+" .env; then
    bad ".env 里有明文凭据(镜像里不该有;应由平台运行时注入)"
  else
    ok ".env 无明文凭据"
  fi
else
  ok "镜像内无 .env(由入口脚本在运行时生成)"
fi

printf '\n通过 %d 项,失败 %d 项\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ] && [ "$BUILD" = "1" ]; then
  echo "构建期审计未通过 → 不出镜像"
  exit 1
fi
exit 0