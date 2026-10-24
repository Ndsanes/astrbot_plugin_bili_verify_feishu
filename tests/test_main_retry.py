"""main 的 UID 复查调度单测（不启动 AstrBot、不联网）。

覆盖：
  - 「未知」→ 后台任务 → 10 分钟后以 retry_unknown=True 复查 → 应用决策
  - 关闭复查（retry_seconds=0）时不再调度
  - 群消息「像 UID 提交」的判定（决定失败时是否出声）
"""

from __future__ import annotations

import pytest

from astrbot_plugin_bili_verify_feishu.admission import AdmissionResult
from astrbot_plugin_bili_verify_feishu.main import BiliVerifyFeishuPlugin
from astrbot_plugin_bili_verify_feishu.platform_port import JoinRequest


class FakeService:
    def __init__(self, result: AdmissionResult):
        self.result = result
        self.calls: list[bool] = []

    async def admit_request(self, req, *, retry_unknown: bool = False):
        self.calls.append(retry_unknown)
        return self.result


def make_req() -> JoinRequest:
    return JoinRequest(
        group_openid="g1",
        member_openid="u1",
        join_request_id="req-1",
        username="nick",
        comment="UID 123456789",
        raw={},
    )


@pytest.mark.asyncio
async def test_复查任务以retry_unknown重跑并应用结果(monkeypatch):
    plugin = BiliVerifyFeishuPlugin(None, {})
    service = FakeService(AdmissionResult(decision="approve", uid="123456789"))
    plugin._admission_service = service
    applied: list[AdmissionResult] = []

    async def fake_apply(result, group, user, join_request_id):
        applied.append(result)

    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(plugin, "_apply_admission_result", fake_apply)
    monkeypatch.setattr(
        "astrbot_plugin_bili_verify_feishu.main.asyncio.sleep", fake_sleep
    )

    req = make_req()
    await plugin._retry_join_admission(req)

    assert slept == [plugin._uid_retry_seconds]
    assert service.calls == [True]
    assert applied and applied[0].decision == "approve"


def test_关闭复查时不调度():
    plugin = BiliVerifyFeishuPlugin(None, {"BILI_UID_CHECK_RETRY_SECONDS": 0})
    plugin._admission_service = FakeService(AdmissionResult("approve", "1"))
    assert plugin._uid_retry_seconds == 0
    assert plugin._schedule_join_retry(make_req()) is False


@pytest.mark.asyncio
async def test_开启复查时调度成功():
    plugin = BiliVerifyFeishuPlugin(None, {})
    plugin._admission_service = FakeService(AdmissionResult("approve", "1"))
    assert plugin._uid_retry_seconds == 600
    assert plugin._schedule_join_retry(make_req()) is True
    # 清理：取消未完成的复查任务，避免 pytest 报 coroutine never awaited
    await plugin.terminate()


def test_像UID提交的判定():
    check = BiliVerifyFeishuPlugin._looks_like_uid_submission
    assert check("UID：123456") is True
    assert check("我的b站号 123456") is True
    assert check(" 123456 ") is True
    assert check("订单 123456789 已到") is False
    assert check("2026-10-24 12:00:00") is False
