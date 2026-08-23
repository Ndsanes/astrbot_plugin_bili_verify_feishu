"""11255「无关联群」重探计时测试。

QQ 官方网关对与本 bot 无关的群返回 err_code=11255（文案误导为"用户/群已注销"）。
main.py 的策略：首次探测到 not_related 时打时间戳标记，窗口
（_QQOFFICIAL_INVALID_RETRY_SECONDS = 1800s）内跳过轮询，过期后自动重探，
兼容"当时无权限、之后才被拉进群"的场景。

通过 __new__ 构造裸实例 + 桩方法驱动真实轮询代码路径（不重构生产逻辑）；
time 用 monkeypatch 控制，asyncio.sleep 置空以消除 0.5s 节流等待。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from astrbot_plugin_bili_verify_feishu import main as main_mod
from astrbot_plugin_bili_verify_feishu.main import BiliVerifyFeishuPlugin

UMO = "default_bot1:GroupMessage:6CCC18AB28098F241B44FF1A41F6668F"
GROUP = "6CCC18AB28098F241B44FF1A41F6668F"
PID = "default_bot1"
WINDOW = BiliVerifyFeishuPlugin._QQOFFICIAL_INVALID_RETRY_SECONDS


def make_plugin() -> BiliVerifyFeishuPlugin:
    """构造绕过 __init__ 的裸插件实例（轮询相关状态与桩由各用例自行装配）。"""
    plugin = BiliVerifyFeishuPlugin.__new__(BiliVerifyFeishuPlugin)
    plugin._qqofficial_invalid_groups = {}
    plugin._group_openid_pid = {}
    return plugin


@pytest.fixture()
def no_sleep(monkeypatch):
    """抹平轮询循环里每群 0.5s 的节流 sleep。"""

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


@pytest.fixture()
def whitelist(monkeypatch):
    def _set(entries: list[str]):
        monkeypatch.setattr(main_mod, "load_whitelist", lambda: list(entries))

    return _set


# --------------------------------------------------------------------------- #
# 首次标记：not_related → 打时间戳
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_首次无关联打时间戳标记():
    plugin = BiliVerifyFeishuPlugin.__new__(BiliVerifyFeishuPlugin)
    plugin._qqofficial_invalid_groups = {}
    plugin._group_openid_pid = {}
    # 完整走 _poll_single_group_join_requests 的真实代码路径：
    # 桩掉 adapter/http 获取与实际 HTTP 探测，令其返回 not_related
    fake_adapter = type("A", (), {"get_client": staticmethod(lambda: "http-client")})()
    plugin._qqofficial_adapter_by_id = lambda pid: fake_adapter  # type: ignore[method-assign]
    plugin._client_to_http = lambda client: client  # type: ignore[method-assign]

    async def fake_via(group_openid, http):
        return "not_related"

    plugin._poll_group_join_requests_via = fake_via  # type: ignore[method-assign]

    before = time.time()
    await plugin._poll_single_group_join_requests(GROUP, PID)

    marked = plugin._qqofficial_invalid_groups.get(GROUP)
    assert marked is not None
    assert marked == pytest.approx(before, abs=5.0)  # 标记值为当下时间戳


# --------------------------------------------------------------------------- #
# 窗口内跳过 / 过期重探：经 _poll_qqofficial_join_requests_once 全循环
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_窗口内跳过轮询(no_sleep, whitelist):
    polled: list[str] = []
    plugin = make_plugin()
    whitelist([UMO])
    plugin._poll_single_group_join_requests = _recorder(polled)  # type: ignore[method-assign]

    # 刚刚标记（窗口内）
    plugin._qqofficial_invalid_groups[GROUP] = time.time() - 10.0
    await plugin._poll_qqofficial_join_requests_once()

    assert polled == []  # 窗口内零动作
    assert GROUP in plugin._qqofficial_invalid_groups  # 标记保留


@pytest.mark.asyncio
async def test_标记过期后允许重探(no_sleep, whitelist):
    polled: list[str] = []
    plugin = make_plugin()
    whitelist([UMO])
    plugin._poll_single_group_join_requests = _recorder(polled)  # type: ignore[method-assign]

    plugin._qqofficial_invalid_groups[GROUP] = time.time() - WINDOW - 1.0
    await plugin._poll_qqofficial_join_requests_once()

    assert polled == [GROUP]  # 过期 → 本轮重探
    assert GROUP not in plugin._qqofficial_invalid_groups  # 过期标记已删除


@pytest.mark.asyncio
async def test_正常条目直接轮询并记录归属(no_sleep, whitelist):
    polled: list[str] = []
    plugin = make_plugin()
    whitelist([UMO])
    plugin._poll_single_group_join_requests = _recorder(polled)  # type: ignore[method-assign]

    await plugin._poll_qqofficial_join_requests_once()

    assert polled == [GROUP]
    # 白名单 UMO 首段写入 openid → 平台实例映射，审批路径据此复用同一 bot
    assert plugin._group_openid_pid[GROUP] == PID


@pytest.mark.asyncio
async def test_无法解析实例的条目跳过(no_sleep, whitelist):
    """纯 openid / 数字群号没有首段实例 ID → 不轮询、不写归属映射。"""
    polled: list[str] = []
    plugin = make_plugin()
    whitelist(["6CCC18AB28098F241B44FF1A41F6668F", "1048195177"])
    plugin._poll_single_group_join_requests = _recorder(polled)  # type: ignore[method-assign]

    await plugin._poll_qqofficial_join_requests_once()

    assert polled == []
    assert plugin._group_openid_pid == {}


@pytest.mark.asyncio
async def test_同openid多条白名单项只拉一次(no_sleep, whitelist):
    polled: list[str] = []
    plugin = make_plugin()
    whitelist([UMO, "default_bot2:GroupMessage:" + GROUP])  # 同群两个 bot 条目
    plugin._poll_single_group_join_requests = _recorder(polled)  # type: ignore[method-assign]

    await plugin._poll_qqofficial_join_requests_once()

    assert polled == [GROUP]


def _recorder(polled: list[str]):
    async def record(group_openid: str, platform_id: str) -> None:
        polled.append(group_openid)

    return record
