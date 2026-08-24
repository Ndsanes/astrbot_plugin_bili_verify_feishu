"""
member_registry — 深 module / 窄 interface / 高 leverage

职责（depth）：
  收口飞书多维表格的成员落库细节，提供领域导向的窄 interface。
  隐藏内部：字段映射（UID/QQ号/昵称/时间/状态）、int|str openid 兼容、
  重试/指数退避、lark_cli 网关调用、app_token/table_id 归一。

对外暴露（seam 之后）：
  MemberRegistry.register(uid, openid, nickname) -> bool

设计取舍（leverage / locality）：
  - leverage：一个 registry 供 Admissions 的 push/poll 两处复用
  - locality：表结构、重试、限流集中一处，调用方不再拼 fields 字典
  - interface 即测试面：可注入 FakeRegistry，无需 mock lark SDK
  - 删除测试：删掉本 module 会使字段映射与重试散回 main 的调用点
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging as _l
    logger = _l.getLogger(__name__)

from .feishu_client import (
    upsert_member_row_by_qq_with_retry as _upsert_with_retry,
)


def _qq_value(qq: int | str) -> int | str:
    if isinstance(qq, str) and qq.isdigit():
        try:
            return int(qq)
        except ValueError:
            return qq
    return qq


class MemberRegistry:
    """
    深 module：成员在飞书多维表格的注册。

    interface 窄（register），implementation 深（字段组装、重试、限流、lark）。
    seam 之后隐藏：表字段名、状态值、时间戳生成、QQ号 text vs int 兼容。
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        qq_field: str = "QQ号",
        status_field: str = "状态",
        status_active: str = "在群",
        status_left: str = "已退群",
    ) -> None:
        self._config = config
        self._qq_field = (qq_field or "QQ号").strip() or "QQ号"
        self._status_field = (status_field or "状态").strip() or "状态"
        self._status_active = (status_active or "在群").strip() or "在群"
        self._status_left = (status_left or "已退群").strip() or "已退群"

    def _build_fields(self, uid: int, openid: int | str, nickname: str) -> dict[str, Any]:
        time_ms = int(datetime.now(UTC).timestamp() * 1000)
        return {
            "UID": int(uid),
            "QQ号": _qq_value(openid),
            "昵称": str(nickname or "").strip(),
            "时间": time_ms,
            self._status_field: self._status_active,
        }

    async def register(self, uid: int, openid: int | str, nickname: str = "") -> bool:
        """注册或更新成员（按 QQ号/openid 去重）。"""
        fields = self._build_fields(int(uid), openid, nickname)
        qv = _qq_value(openid)
        ok = await _upsert_with_retry(
            fields=fields,
            qq_num=qv,  # type: ignore[arg-type]
            config=self._config,
            qq_field_name=self._qq_field,
        )
        if ok:
            logger.info(f"[MemberRegistry] register ok uid={uid} openid={openid} nick={nickname}")
        else:
            logger.error(f"[MemberRegistry] register failed uid={uid} openid={openid}")
        return ok


__all__ = ["MemberRegistry"]
