"""pytest 全局配置：astrbot 不可用时注入最小桩包。

bili_verify 各深模块（admission / admissions_store / platform_port / member_registry）
对 ``from astrbot.api import logger`` 已有 try/except 兜底；但 storage.py 与 main.py
是无条件导入——测试 _split_umo_entry 与 11255 重探计时必须能导入 main，
故保留 astrbot 桩（tests/stubs/astrbot）。

feishu_client 已网关化：不再依赖 lark_oapi，飞书调用经注入的假网关完成
（见 test_feishu_gateway.py），lark_oapi 桩包已删除。
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


if not _has_module("astrbot"):
    sys.path.insert(0, str(STUBS))
