"""pytest 全局配置：astrbot / lark_oapi 不可用时注入最小桩包。

bili_verify 各深模块（admission / admissions_store / platform_port / member_registry）
对 ``from astrbot.api import logger`` 已有 try/except 兜底；但 storage.py 与 main.py
是无条件导入，feishu_client 顶层依赖 lark_oapi——测试 _split_umo_entry 与
11255 重探计时必须能导入 main，故同时提供两套桩（tests/stubs/）。
"""

from __future__ import annotations

import sys
from pathlib import Path

STUBS = Path(__file__).parent / "stubs"


def _has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


if not (_has_module("astrbot") and _has_module("lark_oapi")):
    sys.path.insert(0, str(STUBS))
