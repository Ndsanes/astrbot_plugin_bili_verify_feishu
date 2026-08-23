"""
admission — 深 module / 窄 interface / 高 depth

职责（depth）：
  收口分散在 main.py 的两条重复链路：
  aiocqhttp 推送（_on_group_request）与 qq_official 轮询（_handle_qqofficial_join_request / _poll_* / _qqofficial_approve）
  的共同序列：提取 UID → 组装 → 飞书落库 → 放行/拒绝。

隐藏的内部（seam 之后）：
  - UID 正则与合并数字逻辑
  - Feishu 字段映射与重试（经 MemberRegistry）
  - 去重键（join_request_id / group:user:flag）与 pending 队列
  - 防 gank 随机延迟
  - 拒绝理由与状态回写

对外暴露（窄 interface，2 方法）：
  AdmissionService.admit_request(req: JoinRequest) -> Decision
  AdmissionService.admit_message(group_id, user_id, text, nickname) -> bool

设计取舍（leverage / locality / depth）：
  - leverage：一个 interface，2+ 调用方（OneBot 事件、QQ 官方轮询、群消息补录）
  - locality：审批类 bug（UID 错提、飞书重试、重复放行）集中一处
  - depth：interface 2 方法，实现吸收 5 个 wrapper + 2 个 adapter 差异
  - seam：后依赖 MemberRegistry / AdmissionsStore / PluginConfig / PlatformPort 的 JoinRequest DTO；
    删除测试：删掉本 module 会使 UID→Feishu→Approve 逻辑重新散回 main 的 5 处
"""
from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging as _l
    logger = _l.getLogger(__name__)

from .admissions_store import AdmissionsStore
from .member_registry import MemberRegistry
from .platform_port import JoinRequest
from .plugin_config import PluginConfig

Decision = Literal["approve", "decline", "skip"]


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    decision: Decision
    uid: str | None
    reason: str = ""


UID_RE = re.compile(r"(\d{6,})")


def _extract_uid(text: str) -> str | None:
    s = (text or "").strip()
    m = UID_RE.search(s)
    if m:
        return m.group(1)
    merged = "".join(re.findall(r"\d+", s))
    return merged if len(merged) >= 6 else None


class AdmissionService:
    """
    深 module：入群审批的单一决策面。

    interface 窄（admit_request / admit_message），implementation 深（见模块 docstring）。
    seam 之后隐藏：正则、随机延迟、MemberRegistry、AdmissionsStore、PlatformPort DTO。
    """

    def __init__(
        self,
        registry: MemberRegistry,
        store: AdmissionsStore,
        config: PluginConfig,
    ) -> None:
        self._registry = registry
        self._store = store
        self._config = config

    async def _delay_if_needed(self) -> None:
        dmin = float(self._config.delays.min_seconds or 0)
        dmax = float(self._config.delays.max_seconds or 0)
        if dmax < dmin:
            dmax = dmin
        if dmin <= 0 and dmax <= 0:
            return
        delay = random.uniform(dmin, dmax) if dmax > dmin else dmin
        if delay > 0:
            await asyncio.sleep(delay)

    async def admit_request(self, req: JoinRequest) -> AdmissionResult:
        """
        处理单条入群申请（JoinRequest），完成 UID 校验 → 飞书 → 决策。

        调用方（PlatformPort 适配器或 OneBot 事件）只需传入归一后的 JoinRequest，
        无需知道飞书字段或状态机。
        """
        group = req.group_openid.strip()
        user = req.member_openid.strip()
        comment = req.comment or ""
        key = f"{group}:{user}:{req.join_request_id}"

        # 去重:join_request_id 维度。
        # dedup() 语义:True=首次出现(继续处理),False=重复(跳过)。
        if not self._store.dedup(key):
            logger.debug(f"[Admission] 重复申请跳过 {key}")
            return AdmissionResult(decision="skip", uid=None, reason="duplicate")

        # 白名单由 store 判断（保持 locality）
        if not self._store.is_whitelisted(group):
            logger.info(f"[Admission] 非白名单群忽略 group={group} user={user}")
            return AdmissionResult(decision="decline", uid=None, reason="not_whitelisted")

        await self._delay_if_needed()

        uid = _extract_uid(comment)
        if uid is None:
            logger.info(f"[Admission] 无有效 UID，拒绝 group={group} user={user} comment={comment!r}")
            return AdmissionResult(decision="decline", uid=None, reason="请在入群验证信息中提供B站UID")

        # 飞书落库（经 MemberRegistry 深 module）
        ok = await self._registry.register(int(uid), user, req.username)
        if ok:
            # mark_verified 已同时移除 pending;不得再调 discard_pending,
            # 否则会把 _verified_before_join 一并清掉,group_increase 无法跳过二次待补
            self._store.mark_verified(group, user)
            logger.info(f"[Admission] 入群 UID 写入成功 group={group} user={user} uid={uid}")
            return AdmissionResult(decision="approve", uid=uid)
        else:
            # 失败入 pending，仍放行由补偿处理（保持原策略）
            self._store.enqueue_failed(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "group_id": group,
                    "user_id": user,
                    "uid": uid,
                    "nickname": req.username,
                    "retry_count": 0,
                    "join_request_id": req.join_request_id,
                }
            )
            logger.warning(f"[Admission] 飞书写入失败已入 pending，仍放行 uid={uid} user={user}")
            return AdmissionResult(decision="approve", uid=uid, reason="feishu_pending")

    async def admit_message(self, group_id: str, user_id: str, text: str, nickname: str = "") -> bool:
        """
        处理群内补录消息（qq_official @消息 或 aiocqhttp pending 校验后）。

        无 join_request_id，不做审批，仅做落库；返回是否成功写入。
        """
        group = str(group_id or "").strip()
        user = str(user_id or "").strip()
        if not group or not self._store.is_whitelisted(group):
            return False
        # aiocqhttp 路径需要 pending 校验，qq_official 不需要——由 store 统一判断：
        # pending 模式下未 pending 则忽略，store 可配置；此处按现有策略：
        # 若 store 中无 pending 且为 aiocqhttp 场景，调用方应在外层已判断 is_pending
        uid = _extract_uid(text or "")
        if uid is None:
            return False

        await self._delay_if_needed()
        ok = await self._registry.register(int(uid), user, nickname)
        if ok:
            self._store.discard_pending(group, user)
            logger.info(f"[Admission] 群消息 UID 补录成功 group={group} user={user} uid={uid}")
            return True
        else:
            self._store.enqueue_failed(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "group_id": group,
                    "user_id": user,
                    "uid": uid,
                    "nickname": nickname,
                    "retry_count": 0,
                }
            )
            logger.warning(f"[Admission] 群消息 UID 补录失败已入 pending uid={uid} user={user}")
            return False


__all__ = ["AdmissionService", "AdmissionResult", "JoinRequest"]
