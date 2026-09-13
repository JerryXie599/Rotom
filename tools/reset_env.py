#!/usr/bin/env python3
"""重置容器题环境 —— 仅 interactive=true 的题目可用。

用法:
  TEAM_TOKEN=xxx python3 tools/reset_env.py <question_id>
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import contest_api  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: reset_env.py <question_id>", file=sys.stderr)
        return 2
    token = os.environ.get("TEAM_TOKEN", "")
    if not token:
        print("TEAM_TOKEN 未设置", file=sys.stderr)
        return 2
    try:
        resp = contest_api.reset_env(token, sys.argv[1])
    except Exception as e:
        print(f"RESET_ERROR {e}", file=sys.stderr)
        return 3
    print(resp)
    return 0 if resp.get("code") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
