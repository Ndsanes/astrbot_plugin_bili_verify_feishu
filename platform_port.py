"""
platform_port.py — PlatformPort seam / adapter / depth

窄 interface，藏细节，供 Admission 整合任务串联。

术语：
- module: 本文件
- interface: PlatformPort 协议
- seam: 调用方仅依赖 PlatformPort，不 import botpy / aiocqhttp
- adapter: QqOfficialAdapter / OneBotAdapter 两个真实 adapter
- depth: 内部收口分页、comment 拼接、参数归一、限流 seam
- leverage: 上层复用同一业务流无需分支
- locality: 平台差异隔离在 adapter 内

参考：
- AstrBot 事件监听与平台适配：https://docs.astrbot.app/dev/star/guides/listen-message-event.html
- QQ 官方文档 v2 群接口：https://bot.q.qq.com/wiki/develop/api-v2/
  - GET  /v2/groups/{group_openid}/join_request_list  (cursor, limit, next_cursor)
  - POST /v2/groups/{group_openid}/approval_join_request/{member_openid}
         { op: approve|decline, join_request_id, reject_reason }
  - 频控：查询类 ~30QPM，审批类 ~60QPM 左右，适配器对调用点留好 seam

调用方示例::

    port: PlatformPort = QqOfficialAdapter(client)
    items, next_cursor = await port.list_join_requests(group_openid, cursor="", limit=20)
    ok = await port.approve_join_request(group_openid, m.openid, m.join_request_id, True)

"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

try:
    from astrbot.api import logger as _astr_logger  # type: ignore
    logger = _astr_logger
except Exception:  # pragma: no cover
    logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JoinRequest — 归一后的入群申请 DTO（openid 语义，数字 QQ 转 str）
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class JoinRequest:
    """归一化入群申请。

    Attributes:
        group_openid: 群标识（qq_official group_openid / aiocqhttp group_id，统一 str）
        join_request_id: QQ 官方返回的 join_request_id；OneBot 侧复用 flag/request_id
        member_openid: 申请人 openid / user_id（统一为 str，数字 QQ 也转 str）
        username: 申请人昵称（可能为空，resolve_nickname 可二次解析）
        comment: 校验信息（QQ 官方 verify_info.verify_message 与 review_qa_list 拼接；OneBot 侧 comment/message）
        raw: 原始条目，便于透传 sub_type / verify_info 等
    """

    group_openid: str = ""
    join_request_id: str = ""
    member_openid: str = ""
    username: str = ""
    comment: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# PlatformPort interface — 3 方法 seam
# ---------------------------------------------------------------------------

@runtime_checkable
class PlatformPort(Protocol):
    """平台端口协议。调用方只依赖此 interface，不 import botpy / aiocqhttp。"""

    async def list_join_requests(
        self, group_openid: str, cursor: str = "", limit: int = 20
    ) -> tuple[list[JoinRequest], str]:
        """拉取入群申请列表，返回 (items, next_cursor)。"""
        ...

    async def approve_join_request(
        self,
        group_openid: str,
        member_openid: str,
        join_request_id: str,
        approve: bool,
        reject_reason: str = "",
    ) -> bool:
        """审批单条入群申请。"""
        ...

    # 兼容 contract 中的命名 ``approve`` 与 ``resolve_name``
    async def approve(
        self,
        group_openid: str,
        member_openid: str,
        join_request_id: str,
        approve: bool,
        reject_reason: str = "",
    ) -> bool:  # pragma: no cover - alias
        ...

    async def resolve_nickname(
        self,
        user_id: str,
        event: Any | None = None,
        raw: dict[str, Any] | None = None,
    ) -> str:
        """解析用户昵称，优先 event/raw，兜底远端查询。"""
        ...

    async def resolve_name(  # alias for contract
        self,
        user_id: str,
        event: Any | None = None,
        raw: dict[str, Any] | None = None,
    ) -> str:  # pragma: no cover - alias
        ...


# ---------------------------------------------------------------------------
# 内部 helpers — depth 收口
# ---------------------------------------------------------------------------

def _to_str(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _build_qq_comment(verify_info: Any) -> str:
    """从 QQ 官方 verify_info 提取 comment：verify_message 优先，否则拼接 review_qa_list[].answer。"""
    if not isinstance(verify_info, dict):
        return ""
    msg = _to_str(verify_info.get("verify_message"))
    if msg:
        return msg
    qa_list = verify_info.get("review_qa_list")
    if isinstance(qa_list, list):
        parts: list[str] = []
        for qa in qa_list:
            if isinstance(qa, dict):
                ans = _to_str(qa.get("answer"))
                if ans:
                    parts.append(ans)
        if parts:
            return " ".join(parts)
    return ""


def _extract_onebot_requests(payload: Any) -> list[dict[str, Any]]:
    """从 get_group_system_msg 返回值提取请求列表（兼容多种包裹）。"""
    if isinstance(payload, dict):
        data = payload.get("data", payload)
    else:
        data = payload
    if not isinstance(data, dict):
        return []
    out: list[dict[str, Any]] = []
    for req in data.get("join_requests", []) or []:
        if isinstance(req, dict):
            item = dict(req)
            item.setdefault("sub_type", "add")
            out.append(item)
    for req in data.get("invited_requests", []) or []:
        if isinstance(req, dict):
            item = dict(req)
            item.setdefault("sub_type", "invite")
            out.append(item)
    for req in data.get("requests", []) or []:
        if isinstance(req, dict):
            if req not in out:
                out.append(dict(req))
    # 兜底：某些实现直接返回 list
    if not out and isinstance(data.get("list"), list):
        for req in data.get("list", []):
            if isinstance(req, dict):
                out.append(dict(req))
    return out


# ---------------------------------------------------------------------------
# 限流 seam — 30QPM / 60QPM 调用点预留
# ---------------------------------------------------------------------------

class _NoopRateLimiter:
    """默认不限流，留 seam 供上层注入。"""

    async def acquire(self, key: str = "") -> None:
        return


# ---------------------------------------------------------------------------
# QqOfficialAdapter — botpy.http.Route + BotHttp.request
# ---------------------------------------------------------------------------

class QqOfficialAdapter:
    """QQ 官方群接口适配器。

    依赖 botpy 的 http 客户端，但本模块顶层不 import botpy，
    仅在方法内惰性导入，保持 seam 纯净。

    Args:
        client: botpy.Client 实例，或 None（配合 get_client 惰性获取）
        get_client: () -> client 的可调用，优先级高于 client
        rate_limiter: 可选限流器，需实现 ``async def acquire(key:str)``
            - key ``"list"`` 对应 30QPM 查询
            - key ``"approve"`` 对应 60QPM 审批
            未提供则不做限流，仅保留 seam
    """

    # 文档化频控常量，供外部限流器参考
    LIST_QPM = 30
    APPROVE_QPM = 60

    def __init__(
        self,
        client: Any | None = None,
        get_client: Callable[[], Any] | None = None,
        rate_limiter: Any | None = None,
    ) -> None:
        self._client = client
        self._get_client = get_client
        self._limiter = rate_limiter or _NoopRateLimiter()

    # -- internal: client / http resolution (locality) --------------------

    def _resolve_client(self) -> Any | None:
        if self._get_client is not None:
            try:
                c = self._get_client()
                if c is not None:
                    return c
            except Exception:
                pass
        return self._client

    def _resolve_http(self, client: Any) -> Any | None:
        # 兼容 botpy 版本差异：client.api._http 或 client._http
        http = getattr(getattr(client, "api", None), "_http", None)
        if http is not None:
            return http
        http = getattr(client, "_http", None)
        if http is not None:
            return http
        # 某些封装直接把 request 挂在 api 上
        if hasattr(client, "request"):
            return client
        if hasattr(getattr(client, "api", None), "request"):
            return getattr(client, "api")
        return None

    # -- PlatformPort interface -------------------------------------------

    async def list_join_requests(
        self, group_openid: str, cursor: str = "", limit: int = 20
    ) -> tuple[list[JoinRequest], str]:
        group_openid = _to_str(group_openid)
        cursor = _to_str(cursor)
        try:
            limit = int(limit)
        except Exception:
            limit = 20
        limit = max(1, min(limit, 100))

        client = self._resolve_client()
        if client is None:
            logger.debug("[PlatformPort][qq_official] list: no client")
            return [], ""

        try:
            from botpy.http import Route  # type: ignore
        except Exception:
            logger.warning("[PlatformPort][qq_official] botpy.http.Route not found")
            return [], ""

        http = self._resolve_http(client)
        if http is None:
            logger.warning("[PlatformPort][qq_official] cannot resolve http client")
            return [], ""

        # seam: 30QPM 查询限流点
        try:
            await self._limiter.acquire("list")
        except Exception:
            pass

        route = Route("GET", "/v2/groups/{group_openid}/join_request_list", group_openid=group_openid)
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit

        try:
            if params:
                ret = await http.request(route, params=params)
            else:
                ret = await http.request(route)
        except TypeError:
            # 兼容旧版 botpy 仅支持 json 参数
            try:
                ret = await http.request(route, json=params if params else None)
            except Exception as e:
                logger.debug(f"[PlatformPort][qq_official] list request failed: {e}")
                return [], ""
        except Exception as e:
            logger.debug(f"[PlatformPort][qq_official] list request failed: {e}")
            return [], ""

        if ret is None:
            return [], ""
        payload = ret.get("data", ret) if isinstance(ret, dict) else ret
        if not isinstance(payload, dict):
            return [], ""

        raw_list = payload.get("list", []) or []
        next_cursor = _to_str(payload.get("next_cursor", ""))

        items: list[JoinRequest] = []
        for entry in raw_list:
            if not isinstance(entry, dict):
                continue
            jid = _to_str(entry.get("join_request_id"))
            mid = _to_str(entry.get("member_openid"))
            if not jid or not mid:
                continue
            username = _to_str(entry.get("username"))
            verify_info = entry.get("verify_info", {})
            comment = _build_qq_comment(verify_info)
            # 兜底：某些版本直接顶层带 verify_message
            if not comment:
                comment = _to_str(entry.get("verify_message") or entry.get("comment") or entry.get("message"))
            items.append(
                JoinRequest(
                    join_request_id=jid,
                    member_openid=mid,
                    username=username,
                    comment=comment,
                    raw=dict(entry),
                )
            )
        return items, next_cursor

    async def approve_join_request(
        self,
        group_openid: str,
        member_openid: str,
        join_request_id: str,
        approve: bool,
        reject_reason: str = "",
    ) -> bool:
        group_openid = _to_str(group_openid)
        member_openid = _to_str(member_openid)
        join_request_id = _to_str(join_request_id)
        reject_reason = _to_str(reject_reason)

        if not group_openid or not member_openid or not join_request_id:
            return False

        client = self._resolve_client()
        if client is None:
            logger.error("[PlatformPort][qq_official] approve: no client")
            return False

        try:
            from botpy.http import Route  # type: ignore
        except Exception:
            logger.error("[PlatformPort][qq_official] approve: botpy.http.Route not found")
            return False

        http = self._resolve_http(client)
        if http is None:
            logger.error("[PlatformPort][qq_official] approve: cannot resolve http")
            return False

        # seam: 60QPM 审批限流点
        try:
            await self._limiter.acquire("approve")
        except Exception:
            pass

        route = Route(
            "POST",
            "/v2/groups/{group_openid}/approval_join_request/{member_openid}",
            group_openid=group_openid,
            member_openid=member_openid,
        )
        payload: dict[str, Any] = {
            "op": "approve" if approve else "decline",
            "join_request_id": join_request_id,
        }
        if not approve and reject_reason:
            payload["reject_reason"] = reject_reason

        try:
            ret = await http.request(route, json=payload)
            logger.info(
                f"[PlatformPort][qq_official] approve: approve={approve} "
                f"group={group_openid} member={member_openid} ret={ret}"
            )
            return True
        except Exception as e:
            logger.error(f"[PlatformPort][qq_official] approve failed: {e}")
            return False

    # alias for contract
    async def approve(
        self,
        group_openid: str,
        member_openid: str,
        join_request_id: str,
        approve: bool,
        reject_reason: str = "",
    ) -> bool:
        return await self.approve_join_request(group_openid, member_openid, join_request_id, approve, reject_reason)

    async def resolve_nickname(
        self,
        user_id: str,
        event: Any | None = None,
        raw: dict[str, Any] | None = None,
    ) -> str:
        uid = _to_str(user_id)
        # 1) AstrMessageEvent 优先
        if event is not None:
            try:
                name = event.get_sender_name()  # type: ignore[attr-defined]
                if name and _to_str(name):
                    return _to_str(name)
            except Exception:
                pass
            try:
                sender = getattr(getattr(event, "message_obj", None), "sender", None)
                if sender is not None:
                    for attr in ("nickname", "name", "card"):
                        v = getattr(sender, attr, "")
                        if v and _to_str(v):
                            return _to_str(v)
            except Exception:
                pass
            # qq_official raw 可能挂在 event.message_obj.raw_message 且为对象
            try:
                raw_msg = getattr(getattr(event, "message_obj", None), "raw_message", None)
                if isinstance(raw_msg, dict):
                    for k in ("username", "nick", "name"):
                        if raw_msg.get(k):
                            return _to_str(raw_msg[k])
            except Exception:
                pass

        if raw is None:
            raw = {}
        if isinstance(raw, dict):
            sender = raw.get("sender", {}) if isinstance(raw.get("sender", {}), dict) else {}
            for v in (
                sender.get("card"),
                sender.get("nickname"),
                raw.get("nickname"),
                raw.get("requester_nick"),
                raw.get("requester_nickname"),
                raw.get("user_name"),
                raw.get("nick"),
                raw.get("username"),
            ):
                if v and _to_str(v):
                    return _to_str(v)
            for key in ("author", "member"):
                obj = raw.get(key)
                if isinstance(obj, dict):
                    for v in (obj.get("username"), obj.get("nick"), obj.get("name")):
                        if v and _to_str(v):
                            return _to_str(v)

        # qq_official 无 get_stranger_info，不做远端兜底
        return ""

    async def resolve_name(
        self,
        user_id: str,
        event: Any | None = None,
        raw: dict[str, Any] | None = None,
    ) -> str:
        return await self.resolve_nickname(user_id, event, raw)


# ---------------------------------------------------------------------------
# OneBotAdapter — aiocqhttp (NapCat/OneBot) 封装
# ---------------------------------------------------------------------------

class OneBotAdapter:
    """OneBot (aiocqhttp) 适配器。

    通过 ``aiocqhttp`` 的 ``get_group_system_msg / set_group_add_request / get_stranger_info``
    封装为同 interface，参数归一为 openid 语义（数字 QQ 转 str）。

    Args:
        client: aiocqhttp 客户端（需具备 ``api.call_action``）
        get_client: 惰性获取 client 的可调用
        rate_limiter: 可选限流器 seam
    """

    def __init__(
        self,
        client: Any | None = None,
        get_client: Callable[[], Any] | None = None,
        rate_limiter: Any | None = None,
    ) -> None:
        self._client = client
        self._get_client = get_client
        self._limiter = rate_limiter or _NoopRateLimiter()

    def _resolve_client(self) -> Any | None:
        if self._get_client is not None:
            try:
                c = self._get_client()
                if c is not None:
                    return c
            except Exception:
                pass
        return self._client

    async def list_join_requests(
        self, group_openid: str, cursor: str = "", limit: int = 20
    ) -> tuple[list[JoinRequest], str]:
        """OneBot 侧用 get_group_system_msg 拟合分页（cursor 被忽略，limit 做切片）。"""
        group_openid = _to_str(group_openid)
        cursor = _to_str(cursor)
        try:
            limit = int(limit)
        except Exception:
            limit = 20
        limit = max(1, min(limit, 100))

        client = self._resolve_client()
        if client is None or not hasattr(client, "api"):
            logger.debug("[PlatformPort][onebot] list: no client")
            return [], ""

        try:
            await self._limiter.acquire("list")
        except Exception:
            pass

        try:
            ret = await client.api.call_action("get_group_system_msg")  # type: ignore[attr-defined]
        except Exception as e:
            logger.debug(f"[PlatformPort][onebot] get_group_system_msg failed: {e}")
            return [], ""

        all_requests = _extract_onebot_requests(ret)

        # 按 group_openid 过滤（OneBot 的 group_id 为数字 str，调用方传入即 openid 语义）
        if group_openid:
            filtered = [r for r in all_requests if _to_str(r.get("group_id")) == group_openid]
        else:
            filtered = all_requests

        # OneBot 无 next_cursor，用 limit/cursor 做简易切片 seam（cursor 为 offset）
        try:
            offset = int(cursor) if cursor.isdigit() else 0
        except Exception:
            offset = 0
        page = filtered[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(filtered) else ""

        items: list[JoinRequest] = []
        for req in page:
            # OneBot 字段归一：flag/request_id -> join_request_id, user_id/requester_uin -> member_openid
            jid = _to_str(req.get("flag") or req.get("request_id") or req.get("join_request_id"))
            mid = _to_str(req.get("user_id") or req.get("requester_uin") or req.get("invitor_uin") or req.get("member_openid"))
            if not jid or not mid:
                continue
            # 数字 QQ 转 str 已在 _to_str 完成
            username = _to_str(
                req.get("requester_nick")
                or req.get("requester_nickname")
                or req.get("nickname")
                or req.get("user_name")
                or req.get("username")
                or ""
            )
            comment = _to_str(req.get("comment") or req.get("message") or req.get("verify_message") or "")
            # 若 raw 含 verify_info 也尝试拼接（兼容）
            if not comment and isinstance(req.get("verify_info"), dict):
                comment = _build_qq_comment(req.get("verify_info"))
            items.append(
                JoinRequest(
                    join_request_id=jid,
                    member_openid=mid,
                    username=username,
                    comment=comment,
                    raw=dict(req),
                )
            )
        return items, next_cursor

    async def approve_join_request(
        self,
        group_openid: str,
        member_openid: str,
        join_request_id: str,
        approve: bool,
        reject_reason: str = "",
    ) -> bool:
        # OneBot 审批不需要 group_openid/member_openid，仅需 flag，但保留参数以归一
        _ = group_openid
        _ = member_openid
        join_request_id = _to_str(join_request_id)
        reject_reason = _to_str(reject_reason)
        if not join_request_id:
            return False

        client = self._resolve_client()
        if client is None or not hasattr(client, "api"):
            logger.error("[PlatformPort][onebot] approve: no client")
            return False

        try:
            await self._limiter.acquire("approve")
        except Exception:
            pass

        # 尝试从 raw 推断 sub_type，默认 add
        sub_type = "add"
        # 若调用方通过 raw 传入，可在外层自行处理；此处保持默认

        payload: dict[str, Any] = {
            "flag": join_request_id,
            "sub_type": sub_type,
            "approve": approve,
        }
        if not approve and reject_reason:
            payload["reason"] = reject_reason

        try:
            ret = await client.api.call_action("set_group_add_request", **payload)  # type: ignore[attr-defined]
            logger.info(f"[PlatformPort][onebot] approve: approve={approve} flag={join_request_id} ret={ret}")
            return True
        except Exception as e:
            logger.error(f"[PlatformPort][onebot] approve failed: {e}")
            return False

    async def approve(
        self,
        group_openid: str,
        member_openid: str,
        join_request_id: str,
        approve: bool,
        reject_reason: str = "",
    ) -> bool:
        return await self.approve_join_request(group_openid, member_openid, join_request_id, approve, reject_reason)

    async def resolve_nickname(
        self,
        user_id: str,
        event: Any | None = None,
        raw: dict[str, Any] | None = None,
    ) -> str:
        uid = _to_str(user_id)

        # 1) event 优先
        if event is not None:
            try:
                name = event.get_sender_name()  # type: ignore[attr-defined]
                if name and _to_str(name):
                    return _to_str(name)
            except Exception:
                pass
            try:
                sender = getattr(getattr(event, "message_obj", None), "sender", None)
                if sender is not None:
                    for attr in ("nickname", "name", "card"):
                        v = getattr(sender, attr, "")
                        if v and _to_str(v):
                            return _to_str(v)
            except Exception:
                pass

        if raw is None:
            raw = {}
        if isinstance(raw, dict):
            sender = raw.get("sender", {}) if isinstance(raw.get("sender", {}), dict) else {}
            for v in (
                sender.get("card"),
                sender.get("nickname"),
                raw.get("nickname"),
                raw.get("requester_nick"),
                raw.get("requester_nickname"),
                raw.get("user_name"),
                raw.get("nick"),
                raw.get("username"),
            ):
                if v and _to_str(v):
                    return _to_str(v)
            for key in ("author", "member"):
                obj = raw.get(key)
                if isinstance(obj, dict):
                    for v in (obj.get("username"), obj.get("nick"), obj.get("name")):
                        if v and _to_str(v):
                            return _to_str(v)

        # 2) OneBot 兜底：get_stranger_info（仅数字 QQ）
        client = self._resolve_client()
        if client is not None and hasattr(client, "api") and uid.isdigit():
            try:
                ret = await client.api.call_action("get_stranger_info", user_id=int(uid))  # type: ignore[attr-defined]
                payload = ret.get("data", ret) if isinstance(ret, dict) else {}
                if isinstance(payload, dict):
                    nick = _to_str(payload.get("nickname"))
                    if nick:
                        return nick
                    # 兼容部分实现 nickname 在顶层
                    nick2 = _to_str(payload.get("card"))
                    if nick2:
                        return nick2
            except Exception:
                return ""
        return ""

    async def resolve_name(
        self,
        user_id: str,
        event: Any | None = None,
        raw: dict[str, Any] | None = None,
    ) -> str:
        return await self.resolve_nickname(user_id, event, raw)


__all__ = [
    "JoinRequest",
    "PlatformPort",
    "QqOfficialAdapter",
    "OneBotAdapter",
]
