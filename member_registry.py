"""
member_registry — 深 module / 窄 interface / 高 leverage

职责（depth）：
  收口飞书多维表格的成员落库细节，提供领域导向的窄 interface。
  隐藏内部：字段映射（UID/QQ号/昵称/时间/状态）、int|str openid 兼容、
  重试/指数退避、lark_cli 网关调用、app_token/table_id 归一。

对外暴露（seam 之后）：
  MemberRegistry.register(uid, openid, nickname) -> bool
  MemberRegistry.mark_left(openid) -> bool
  MemberRegistry.find(openid) -> Optional[str]  # record_id

设计取舍（leverage / locality）：
  - leverage：一个 registry 供 Admissions 的 push/poll/群消息三处复用
  - locality：表结构、重试、限流集中一处，调用方不再拼 fields 字典
  - interface 即测试面：可注入 FakeRegistry，无需 mock lark SDK
  - 删除测试：删掉本 module 会使字段映射与重试散回 main 的 3 个调用点
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
    update_member_status_by_qq_with_retry as _update_status_with_retry,
)
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
    深 module：成员在飞书多维表格的注册与状态。

    interface 窄（3 方法），implementation 深（字段组装、重试、限流、lark）。
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

    @classmethod
    def from_plugin_config(cls, cfg: Any) -> MemberRegistry:
        """从 Typed Config 构造（兼容 PluginConfig 或 dict）。"""
        try:
            # 尝试按 PluginConfig dataclass 读取
            feishu = getattr(cfg, "feishu", None)
            if feishu is not None:
                return cls(
                    config=getattr(cfg, "_raw", {}) or {},
                    qq_field=getattr(feishu, "qq_field", "QQ号"),
                    status_field=getattr(feishu, "status_field", "状态"),
                    status_active=getattr(feishu, "status_active", "在群"),
                    status_left=getattr(feishu, "status_left", "已退群"),
                )
        except Exception:
            pass
        # 回落：直接当 Mapping
        if isinstance(cfg, Mapping):
            return cls(
                config=cfg,
                qq_field=str(cfg.get("FEISHU_QQ_FIELD", "QQ号") or "QQ号"),
                status_field=str(cfg.get("FEISHU_STATUS_FIELD", "状态") or "状态"),
                status_active=str(cfg.get("FEISHU_STATUS_ACTIVE_VALUE", "在群") or "在群"),
                status_left=str(cfg.get("FEISHU_STATUS_LEFT_VALUE", "已退群") or "已退群"),
            )
        return cls(config={})

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

    async def mark_left(self, openid: int | str) -> bool:
        """标记成员已退群。"""
        qv = _qq_value(openid)
        ok = await _update_status_with_retry(
            qq_num=qv,  # type: ignore[arg-type]
            status_value=self._status_left,
            config=self._config,
            qq_field_name=self._qq_field,
            status_field_name=self._status_field,
        )
        if ok:
            logger.info(f"[MemberRegistry] mark_left ok openid={openid}")
        else:
            logger.warning(f"[MemberRegistry] mark_left failed openid={openid}")
        return ok

    async def find(self, openid: int | str) -> str | None:
        """按 openid 查找记录（仅作 seam 预留，当前实现透传 feishu_client）。"""
        # 为保持窄 interface，暂不暴露底层 search；需要时可扩展
        from .feishu_client import _find_record_id_by_qq  # type: ignore

        qv = _qq_value(openid)
        ok, rid = await _find_record_id_by_qq(qv, self._config, self._qq_field)  # type: ignore[arg-type]
        return rid if ok and rid else None


__all__ = ["MemberRegistry"]
