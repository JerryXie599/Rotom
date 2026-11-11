"""文本编码工具:黑板(SHARED.md)是 agent 用 shell 写的,可能混进非 UTF-8 字节。

实测(2026-09-27 那轮):agent 把 GBK 工具输出直接 `>>` 进 SHARED.md,harness 严格 utf-8 解码直接崩
(`UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbc`,连崩把 runner 打到重启循环);
后来改成 `errors="ignore"` 能跑了,但**内容是静默丢掉的**(0xbc 这类字节连同字符一起没了)。

这里的策略是"逐行解码 + 转码",而不是丢字节:
  1. 整段先尝试 UTF-8;
  2. 失败的行单独按 GB18030(GBK/GB2312 的超集,覆盖中文 Windows 工具输出)解码;
  3. 再失败才用 replace 兜底。
输出**永远是合法 UTF-8**,内容尽量保住;调用方原样写回文件即完成了"统一编码"。

另外 harness 写文件一律显式 `encoding="utf-8"`(不要依赖系统 locale),worker 进程也统一注入
UTF-8 环境变量(见 solver.launch),从源头减少 GBK 输出。
"""

from __future__ import annotations

from pathlib import Path

# 逐行尝试的编码顺序:UTF-8 → GB18030 → 替换兜底
_FALLBACK_ENCODINGS = ("gb18030",)


def decode_mixed(data: bytes) -> str:
    """把可能混着 GBK 的字节流解成合法 UTF-8 文本(内容优先,绝不抛异常)。"""
    try:                                   # 整段是干净 UTF-8 时走快路径(最常见)
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    out: list[str] = []
    for line in data.split(b"\n"):
        try:
            out.append(line.decode("utf-8"))
            continue
        except UnicodeDecodeError:
            pass
        for enc in _FALLBACK_ENCODINGS:
            try:
                out.append(line.decode(enc))
                break
            except UnicodeDecodeError:
                continue
        else:
            out.append(line.decode("utf-8", "replace"))    # 兜底:只丢极少数解析不了的字节
    return "\n".join(out)


def read_mixed(path: "str | Path") -> tuple[str, bool]:
    """读文本,返回 (文本, 是否发生过转码)。文件不存在/读不了返回 ("", False)。"""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return "", False
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return decode_mixed(raw), True


def read_text_lossless(path: "str | Path") -> str:
    return read_mixed(path)[0]


def is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False