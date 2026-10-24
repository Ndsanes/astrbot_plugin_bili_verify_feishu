"""
bili_uid — B 站 UID 存在性校验（窄 interface / 深实现）

为什么需要这个 module：
  入群备注里的 UID 只经过 `\\d{6,}` 正则提取，格式合法不等于账号真实存在。
  用户可能抄错、填了 QQ 号、或填了已注销/被封禁的 UID；这些记录一旦写进
  飞书就变成脏数据，且入群审批也放错了人。

对外接口（1 个方法）：
    checker = BiliUidChecker(enabled=True, timeout=5.0, cache_ttl=3600)
    result = await checker.check(123456)
    result.status  # "exists" | "not_found" | "unknown"
    result.name    # status == "exists" 时的 B 站昵称

判定口径（重要，调用方依赖它做决策）：
    - exists    : B 站接口明确返回该用户（code=0 且卡片带 mid）
    - not_found : B 站接口明确返回 -404（用户不存在 / 已注销 / 不可见）
    - unknown   : 超时、网络错误、风控（-352）、返回结构异常等
                  调用方先暂缓审批，隔 `BILI_UID_CHECK_RETRY_SECONDS`（默认 600 秒）
                  复查一次；仍未知才放行——既不立刻放行假 UID，也不把真实用户
                  永远挡在门外。

数据源：
    GET https://api.bilibili.com/x/web-interface/card?mid=<uid>
      code  0    -> data.card.{mid,name}
      code -404  -> 不存在（message: 啥都木有）
      其他 code  -> unknown（-352 风控、-412 请求被拦截等）

实现取舍：
    - 零第三方依赖：stdlib urllib + `asyncio.to_thread`，不阻塞事件循环，
      与仓库「插件无第三方依赖」的约定一致（开发原则只禁止 requests）。
    - 进程内缓存：同一 UID 在 TTL 内不重复请求；unknown 不缓存，避免把
      一次网络抖动固化成「不存在」。负数结果缓存时间更短（默认 10 分钟）。
    - 可注入 fetcher：测试零网络，直接返回构造好的 JSON。
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - 离线测试回退
    import logging as _logging

    logger = _logging.getLogger(__name__)

UidStatus = Literal["exists", "not_found", "unknown"]

CARD_API = "https://api.bilibili.com/x/web-interface/card?mid={uid}"
# 浏览器 UA + Referer：B 站对裸 urllib 请求会更容易触发风控（-352）
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_REFERER = "https://www.bilibili.com/"

# B 站「用户不存在」的业务码；其余非 0 码一律视为暂时性故障
NOT_FOUND_CODE = -404
NEGATIVE_CACHE_TTL = 600


@dataclass(frozen=True, slots=True)
class UidCheck:
    """一次 UID 校验的结果。status 决定调用方是拒绝、放行还是照常处理。"""

    status: UidStatus
    uid: int
    name: str = ""
    detail: str = ""

    @property
    def exists(self) -> bool:
        return self.status == "exists"

    @property
    def not_found(self) -> bool:
        return self.status == "not_found"


class BiliUidChecker:
    """B 站 UID 存在性校验器（进程内缓存，线程池发起 HTTP）。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        timeout: float = 5.0,
        cache_ttl: int = 3600,
        fetcher: Callable[[int], dict[str, Any]] | None = None,
    ) -> None:
        self.enabled = enabled
        self.timeout = max(float(timeout or 0.0), 0.5)
        self.cache_ttl = max(int(cache_ttl or 0), 0)
        self._fetcher = fetcher or self._fetch_json
        # uid -> (结果, 过期时间戳)；只缓存 exists / not_found
        self._cache: dict[int, tuple[UidCheck, float]] = {}

    async def check(self, uid: int) -> UidCheck:
        """校验 UID 是否存在。永不抛异常，最坏返回 unknown。"""
        try:
            uid_num = int(uid)
        except (TypeError, ValueError):
            return UidCheck("not_found", 0, detail="invalid uid")

        if uid_num <= 0:
            return UidCheck("not_found", uid_num, detail="invalid uid")
        if not self.enabled:
            return UidCheck("unknown", uid_num, detail="disabled")

        cached = self._cache_get(uid_num)
        if cached is not None:
            return cached

        try:
            payload = await asyncio.to_thread(self._fetcher, uid_num)
        except Exception as exc:  # 超时/网络/解析异常都放行
            logger.warning(f"[BiliUid] 校验 UID={uid_num} 失败（按未知放行）: {exc}")
            return UidCheck("unknown", uid_num, detail=f"{type(exc).__name__}: {exc}")

        result = self._interpret(uid_num, payload)
        if result.status != "unknown":
            self._cache_put(uid_num, result)
        return result

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #

    def _interpret(self, uid: int, payload: Any) -> UidCheck:
        if not isinstance(payload, dict):
            return UidCheck("unknown", uid, detail="non-object response")

        code = payload.get("code")
        if code == NOT_FOUND_CODE:
            return UidCheck("not_found", uid, detail=str(payload.get("message") or ""))

        if code != 0:
            # -352 风控 / -412 拦截 / -509 限流等：都只是「这次没查到」
            return UidCheck("unknown", uid, detail=f"code={code}")

        data = payload.get("data")
        card = data.get("card") if isinstance(data, dict) else None
        if not isinstance(card, dict):
            return UidCheck("unknown", uid, detail="missing card")
        mid = card.get("mid")
        if mid is None:
            return UidCheck("unknown", uid, detail="missing mid")

        name = str(card.get("name") or "").strip()
        return UidCheck("exists", uid, name=name)

    def _cache_get(self, uid: int) -> UidCheck | None:
        if self.cache_ttl <= 0:
            return None
        entry = self._cache.get(uid)
        if entry is None:
            return None
        result, expires_at = entry
        if time.monotonic() >= expires_at:
            self._cache.pop(uid, None)
            return None
        return result

    def _cache_put(self, uid: int, result: UidCheck) -> None:
        if self.cache_ttl <= 0:
            return
        ttl = self.cache_ttl if result.status == "exists" else min(
            self.cache_ttl, NEGATIVE_CACHE_TTL
        )
        self._cache[uid] = (result, time.monotonic() + ttl)

    def clear_cache(self) -> None:
        self._cache.clear()

    def _fetch_json(self, uid: int) -> dict[str, Any]:
        request = urllib.request.Request(
            CARD_API.format(uid=uid),
            headers={"User-Agent": _USER_AGENT, "Referer": _REFERER},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read()
        return json.loads(body.decode("utf-8", "replace"))


__all__ = ["BiliUidChecker", "UidCheck", "UidStatus"]
