"""策略包(profile):把"CTF/众测"与"正式渗透"的策略差异做成数据,不放代码。

一个 profile = 一份 `profiles/<name>.json`,描述四件事:
  goal        目标与记账方式:flags(提交 flag,平台记账) / findings(记录漏洞,交付报告)
  task_source 任务从哪来:留空=平台适配器(CONTEST_ADAPTER);"targets_api"=本地 targets.json
  dispatch    派发策略:同题默认几个 agent、增援上限(profile 声明即生效,优先级高于 .env)
  containers  容器策略:是否由 harness 管理容器名额、是否时间片轮换、是否做 VPN 预检
  prompt_pack 提示词包名(见 prompts/<pack>/ 与 prompts/base/),(身份/红线/工具链/目标/结束条件)

切换方式:`PROFILE=ctf|pentest`(写在 .env,或网页控制台「运行参数」里的下拉框)。

同一套机制代码 + 不同数据包 = 两种策略;加新模式只需加 json + prompt 片段,不改 runner。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILES_DIR = ROOT / "profiles"
# 当前项目的数据目录(与 board.py / solver.py 同一约定):项目级 meta.json 从这里找
RUN_DIR = Path(os.environ.get("ROTOM_RUN_DIR") or ROOT)
# 默认策略包:本仓库是 Rotom 主项目,默认 "zhongce"(众测/跑分场景,与改造前行为一致);
# CTF 比赛用 PROFILE=ctf,正式渗透用 PROFILE=pentest。
DEFAULT_PROFILE = "zhongce"

_cache: dict[str, "Profile"] = {}


class Profile:
    def __init__(self, name: str, data: dict):
        self.name = name
        self.data = data or {}

    def get(self, path: str, default=None):
        """按点号路径取值,例如 get("containers.rotate", False)。"""
        cur = self.data
        for key in path.split("."):
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur[key]
        return cur

    # ---- 常用字段的语义化读取(避免各处散落字符串路径) ----
    @property
    def title(self) -> str:
        return str(self.get("title") or self.name)

    @property
    def goal(self) -> str:
        return str(self.get("goal") or "flags")

    @property
    def task_source(self) -> str:
        return str(self.get("task_source") or "")

    @property
    def prompt_pack(self) -> str:
        return str(self.get("prompt_pack") or self.name)

    @property
    def targets_file(self) -> str:
        return str(self.get("targets_file") or "targets.json")

    @property
    def system_prompt(self) -> str:
        """pi 的系统提示词文件(相对 prompts/):给 pi 用 --append-system-prompt 追加。

        默认取 <包>/system.md;profile 可以显式指向共用文件(例如 ctf 与众测共用 shared/system-ctf.md)。
        """
        return str(self.get("system_prompt") or f"{self.prompt_pack}/system.md")

    def playbooks(self) -> dict:
        return dict(self.get("playbooks") or {})

    # ---- 循环形状(跑分特化):终止条件 / 分档预算 / 链式钉死 ----
    @property
    def loop_mode(self) -> str:
        """循环以什么为界:work = 任务驱动(只有四种终止条件);window = 时间窗口到点收工(比赛制)。"""
        return str(self.get("loop.mode") or "work")

    def stop_conditions(self) -> set[str]:
        """允许的终止原因集合(默认四种:全部完成/平台不可恢复/网络连续失败/token 无效)。"""
        v = self.get("loop.stop")
        if isinstance(v, (list, tuple)) and v:
            return {str(x) for x in v}
        return {"goal_done", "platform_dead", "net_gone", "token_bad"}

    @property
    def prompt_time_budget(self) -> bool:
        """是否把"本场剩余时间"灌进 worker 提示词(跑分包默认关:不以时间点催 agent 换面)。"""
        return bool(self.get("loop.prompt_time_budget", True))

    def session_budget(self, entry: dict | None) -> int:
        """单个会话的时间预算(秒);0 = 没配,回退到 .env 的 WORKER_TIMEOUT 老逻辑。

        先看链式题覆盖(flag_count>=N → session_s),再看难度分档。数字都从实测来:
        easy 题 11~13 分钟才摸到 RCE;链式题 4 个 flag 需要 ≥90 分钟连续时间。
        """
        entry = entry or {}
        chain = self.get("budgets.chain") or {}
        try:
            need = int(chain.get("flag_count_gte") or 0)
            if need and int(entry.get("flag_count") or 1) >= need:
                return int(chain.get("session_s") or 0)
        except (TypeError, ValueError):
            pass
        by_diff = self.get("budgets.by_difficulty") or {}
        diff = str(entry.get("difficulty") or "").strip().lower()
        if not diff:
            return 0
        try:
            return int(by_diff.get(diff) or 0)
        except (TypeError, ValueError):
            return 0

    def chain_max_attempts(self) -> int:
        """链式题钉死的安全阀:试满这么多次还没进展就允许让位(默认 4;0=永不放弃)。

        没有这条,一道"环境内不可达"的链式题会永远占着 1/3 的容器名额(实测第三次测试里 c-02 就是这类)。
        """
        try:
            return int((self.get("budgets.chain") or {}).get("max_attempts") or 4)
        except (TypeError, ValueError):
            return 4

    def chain_max_pinned_slots(self) -> int:
        """**同时**最多允许几道链式题"钉死容器名额"。

        为什么要有上限:第六次 Docker 测试实测 —— 3 道链式题(b-01/b-02/b-03)各钉一个名额 = 把
        平台仅有的 3 个名额占满,广度题(b-01 解出前)一道都排不上;而广度占 82% 的 flag。
        上限留出至少 1 个名额给广度,链式题照样享受"不被时间片轮换"的保护(只是不能全占)。
        """
        try:
            return int((self.get("budgets.chain") or {}).get("max_pinned_slots") or 1)
        except (TypeError, ValueError):
            return 1

    def kb_extra_dirs(self) -> list[str]:
        """跨轮战果目录:填了就以 `<目录名>/` 前缀进知识库(kb.py 的 KB_EXTRA)。

        上一轮的逐题结论(利用链/凭据/flag 落点)是跨轮最大杠杆(第三次测试实测:三道链式题
        照着上一轮资料全拿)。这里只做"挂载",内容由使用者自己准备;空列表 = 不启用。

        环境变量 ROTOM_KB_EXTRA(冒号分隔)追加在策略包声明之后 —— 项目相关但**不宜写进
        仓库**的本机路径(如涉诈项目的武器库/总控资料)用它注入,策略包 json 保持可公开。
        """
        v = self.get("kb_extra")
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, (list, tuple)):
            v = []
        items = [str(x) for x in v]
        items += [x for x in (os.environ.get("ROTOM_KB_EXTRA") or "").split(os.pathsep) if x.strip()]
        out = []
        for item in items:
            p = Path(item.strip()).expanduser()
            if p.is_dir() and str(p) not in out:
                out.append(str(p))
        return out

    def extensions(self) -> list[str]:
        """pi 扩展文件列表(`--extension`):策略包声明的程序强制行为(如出口合规闸门)。

        相对路径按仓库根解析。pi 正常启动带 --no-extensions;声明了扩展时改为逐个挂载,
        其余发现仍然关闭(不加载用户/项目级扩展,行为可预期)。
        """
        v = self.get("extensions")
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, (list, tuple)):
            return []
        out = []
        for item in v:
            p = Path(str(item)).expanduser()
            if not p.is_absolute():
                p = ROOT / p
            out.append(str(p))
        return out

    def is_pinned(self, entry: dict | None) -> bool:
        """链式题钉死:占住容器名额后不被时间片轮换(避免"容器一关、链条清零"的活锁)。

        只对配置了 budgets.chain 且 flag_count 达标的题生效;`flag_gte<=0` 表示不启用。
        """
        chain = self.get("budgets.chain") or {}
        if not chain.get("pin_slot"):
            return False
        try:
            need = int(chain.get("flag_count_gte") or 0)
        except (TypeError, ValueError):
            return False
        if need <= 0:
            return False
        try:
            return int((entry or {}).get("flag_count") or 1) >= need
        except (TypeError, ValueError):
            return False

    def containers_health(self) -> dict:
        """容器服务级探活配置(`containers.health`)。

        平台的 container_status 只说明"资源占着",不代表服务活着 —— 实测 c-02 被同步 git clone
        阻塞死之后,平台一直报 available,harness 于是永远不换容器。开了这层就会:TCP 通且(像 HTTP
        的端口)有 HTTP 响应才算活,连续 `fail_threshold` 次判死 → 强制 close 换新容器。
        没配这个字段的包(ctf / pentest 容器不由 harness 管)默认关闭。
        """
        return self.get("containers.health") or {}

    def endgame(self) -> dict:
        """收尾期自动增援配置(`dispatch.endgame`)。

        实测第四次跑分:全场只剩 c-02 一题时,5 个并发位里 4 个空着,而它只有一个 agent 在磨
        —— 增援是人工在收工前 14 分钟才加上去的。这里把它自动化:剩下的未解题不多、并发位有余、
        且这道题跑了一阵子还没出新 flag → 自动把同题 agent 数放宽到 max_helpers(带分工简报)。
        """
        return self.get("dispatch.endgame") or {}

    def observer_policy(self) -> dict:
        """Observer 催促策略(`observer`)。

        `mode=steps` 是旧行为(每 KB_NUDGE_STEPS 步催一次查知识库);`mode=progress` 换成
        **产出判据**:长时间没有任何"实质进展"(新 flag / 黑板新结论 / 换人接手)或**反复执行
        完全相同的命令**才催一次,且边沿触发 + 冷却 + 每题上限。长链路场景(众测/正式渗透)
        用 progress,短周期 CTF 保留 steps。
        """
        return self.get("observer") or {}

    def dispatch_int(self, key: str, env_name: str, default: int) -> int:
        """读并发/派发类整数参数:**策略包声明优先**,其次环境变量,最后内置默认。

        旋钮属于"策略"的(同题几个 agent、增援上限)以策略包为准 —— 换 profile 时
        策略自动跟随,不会被上一场留在 .env 里的值带偏;并发/超时/窗口这类"运行"参数
        仍然只从 .env(网页控制台)读。
        """
        v = self.get(f"dispatch.{key}")
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
        raw = (os.environ.get(env_name) or "").strip()
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
        return default

    def role_for(self, adapter: str) -> str:
        m = self.get("role_by_adapter") or {}
        return str(m.get(adapter) or m.get("default") or "顶级选手")

    def event_for(self, adapter: str) -> str:
        m = self.get("event_by_adapter") or {}
        return str(m.get(adapter) or m.get("default") or "一次挑战")


def _load_file(name: str) -> Profile:
    p = PROFILES_DIR / f"{name}.json"
    if not p.exists():
        raise FileNotFoundError(f"策略包不存在:{p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("name") and data["name"] != name:
        raise ValueError(f"策略包 {p} 里的 name={data['name']} 与文件名不一致")
    return Profile(name, data)


def load(name: str | None = None) -> Profile:
    """按名字加载(带缓存);name 为空时按 **项目 meta.json → 环境变量 PROFILE → 默认** 解析。

    项目级优先是为了让"创建项目时选的策略包"真正生效:项目的数据目录里 meta.json 存着 profile,
    runner 在项目目录下启动时读它(切项目 = 切策略,不受全局 .env 影响)。
    """
    name = (name or _project_profile() or os.environ.get("PROFILE") or DEFAULT_PROFILE).strip() or DEFAULT_PROFILE
    if name not in _cache:
        _cache[name] = _load_file(name)
    return _cache[name]


def _project_profile() -> str:
    """当前数据目录(RUN_DIR)里 meta.json 的 profile 字段;没有则返回空串。

    注意不要用宽 except 把实现错误也吞掉(实测坑:这里漏定义 RUN_DIR,NameError 被
    静默当成"没有项目策略包",项目级设置一直不生效)——只包住文件与 JSON 解析。
    """
    meta = RUN_DIR / "meta.json"
    if not meta.exists():
        return ""
    try:
        data = json.loads(meta.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return ""
    return str(data.get("profile") or "").strip()


def current() -> Profile:
    return load()


def names() -> list[str]:
    """可用策略包列表(给控制台下拉框/体检用),默认包排最前。"""
    out = sorted(p.stem for p in PROFILES_DIR.glob("*.json"))
    if DEFAULT_PROFILE in out:
        out.remove(DEFAULT_PROFILE)
        out.insert(0, DEFAULT_PROFILE)
    return out


def describe(name: str) -> str:
    try:
        p = load(name)
    except Exception:
        return ""
    return str(p.get("description") or "")