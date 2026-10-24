"""BiliUidChecker 单测：判定口径、缓存与失败放行。

全程零网络：通过注入 fetcher 返回构造好的 B站响应。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from astrbot_plugin_bili_verify_feishu.bili_uid import BiliUidChecker
from astrbot_plugin_bili_verify_feishu.plugin_config import PluginConfig


def _card(uid: int, name: str = "测试用户") -> dict:
    return {"code": 0, "data": {"card": {"mid": uid, "name": name}}}


class Counter:
    def __init__(self, payload=None, exc: Exception | None = None):
        self.payload = payload
        self.exc = exc
        self.calls: list[int] = []

    def __call__(self, uid: int):
        self.calls.append(uid)
        if self.exc is not None:
            raise self.exc
        return self.payload


@pytest.mark.asyncio
async def test_exists_返回昵称():
    checker = BiliUidChecker(fetcher=lambda uid: _card(uid, "碧诗"))
    result = await checker.check(2)
    assert result.status == "exists"
    assert result.name == "碧诗"
    assert result.exists and not result.not_found


@pytest.mark.asyncio
async def test_not_found_明确不存在():
    checker = BiliUidChecker(
        fetcher=lambda uid: {"code": -404, "message": "啥都木有", "ttl": 1}
    )
    result = await checker.check(999999999999)
    assert result.status == "not_found"
    assert result.not_found
    assert "啥都木有" in result.detail


@pytest.mark.asyncio
async def test_风控码按未知放行():
    checker = BiliUidChecker(fetcher=lambda uid: {"code": -352, "message": "风控校验失败"})
    result = await checker.check(123456)
    assert result.status == "unknown"


@pytest.mark.asyncio
async def test_网络异常按未知放行():
    checker = BiliUidChecker(fetcher=Counter(exc=TimeoutError("timed out")))
    result = await checker.check(123456)
    assert result.status == "unknown"
    assert "TimeoutError" in result.detail


@pytest.mark.asyncio
async def test_返回结构异常按未知放行():
    malformed = (
        "not-a-dict",
        {"code": 0},
        {"code": 0, "data": {}},
        {"code": 0, "data": {"card": {}}},
    )
    for payload in malformed:
        checker = BiliUidChecker(fetcher=lambda uid, p=payload: p)
        assert (await checker.check(123456)).status == "unknown", payload


@pytest.mark.asyncio
async def test_非法UID不请求接口():
    fetcher = Counter(_card(1))
    checker = BiliUidChecker(fetcher=fetcher)
    for uid in (0, -1):
        assert (await checker.check(uid)).status == "not_found"
    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_关闭后不请求():
    fetcher = Counter(_card(1))
    checker = BiliUidChecker(enabled=False, fetcher=fetcher)
    result = await checker.check(123456)
    assert result.status == "unknown"
    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_缓存命中不重复请求():
    fetcher = Counter(_card(2))
    checker = BiliUidChecker(fetcher=fetcher)
    first = await checker.check(2)
    second = await checker.check(2)
    assert first == second
    assert fetcher.calls == [2]


@pytest.mark.asyncio
async def test_负数结果也缓存():
    fetcher = Counter({"code": -404})
    checker = BiliUidChecker(fetcher=fetcher)
    assert (await checker.check(1)).status == "not_found"
    assert (await checker.check(1)).status == "not_found"
    assert fetcher.calls == [1]


@pytest.mark.asyncio
async def test_未知结果不缓存():
    fetcher = Counter(exc=OSError("network"))
    checker = BiliUidChecker(fetcher=fetcher)
    await checker.check(1)
    await checker.check(1)
    assert fetcher.calls == [1, 1]


@pytest.mark.asyncio
async def test_TTL为零关闭缓存():
    fetcher = Counter(_card(2))
    checker = BiliUidChecker(cache_ttl=0, fetcher=fetcher)
    await checker.check(2)
    await checker.check(2)
    assert fetcher.calls == [2, 2]


def test_真实接口解析_不联网():
    """用真实返回样本验证字段解析（不发请求）。"""
    sample = json.loads(
        '{"code":0,"message":"0","ttl":1,"data":{"card":{"mid":2,"name":"碧诗",'
        '"approve":0,"sex":"男","rank":"10000","face":""}}}'
    )
    checker = BiliUidChecker(fetcher=lambda uid: sample)
    result = asyncio.run(checker.check(2))
    assert result.status == "exists"
    assert result.name == "碧诗"


def test_配置解析_UID校验三项带默认与钳制():
    cfg = PluginConfig.from_dict({})
    assert cfg.bili.check_enabled is True
    assert cfg.bili.timeout == 5.0
    assert cfg.bili.cache_ttl == 3600
    assert cfg.bili.retry_seconds == 600

    cfg = PluginConfig.from_dict(
        {
            "BILI_UID_CHECK": "false",
            "BILI_UID_CHECK_TIMEOUT": 0.1,
            "BILI_UID_CHECK_CACHE_TTL": -5,
            "BILI_UID_CHECK_RETRY_SECONDS": 0,
        }
    )
    assert cfg.bili.check_enabled is False
    assert cfg.bili.timeout == 0.5  # 下限钳制
    assert cfg.bili.cache_ttl == 0  # 最小 0
    assert cfg.bili.retry_seconds == 0
    assert cfg.get("BILI_UID_CHECK") is False
    assert cfg.as_dict()["BILI_UID_CHECK_TIMEOUT"] == 0.5
