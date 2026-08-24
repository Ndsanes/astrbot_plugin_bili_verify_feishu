"""AdmissionService 决策域测试。

覆盖 admit_request 的每个 Decision 分支与 _extract_uid 纯函数。
飞书经 FakeRegistry 替身注入，全程零网络。

历史备注（两个疑似 bug 已于 v0.0.6 修复，用例改为断言正确语义）：
    #1 dedup 语义反转——admit_request 曾把 dedup() 首见 True 当"重复命中"
    短路放行，导致首次申请绕过 UID 校验/白名单/飞书；现首次走完整流程、
    重复返回 decision="skip"。
    #2 成功路径 mark_verified 后又调 discard_pending 清掉 verified 标记；
    现只调 mark_verified（其本身已移除 pending），group_increase 可跳过二次待补。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from astrbot_plugin_bili_verify_feishu.admission import (
    AdmissionResult,
    AdmissionService,
    _extract_uid,
)
from astrbot_plugin_bili_verify_feishu.admissions_store import AdmissionsStore
from astrbot_plugin_bili_verify_feishu.platform_port import JoinRequest
from astrbot_plugin_bili_verify_feishu.plugin_config import PluginConfig

# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


class FakeRegistry:
    """MemberRegistry 替身：记录调用、可控制成败。"""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[tuple[int, str, str]] = []

    async def register(self, uid: int, user: str, nickname: str) -> bool:
        self.calls.append((uid, user, nickname))
        return self.ok


@dataclass(slots=True)
class Harness:
    service: AdmissionService
    store: AdmissionsStore
    registry: FakeRegistry


_ZERO_DELAY = {"REQUEST_DELAY_MIN_SECONDS": 0, "REQUEST_DELAY_MAX_SECONDS": 0}


def make_harness(tmp_path, registry: FakeRegistry | None = None, groups=("g1",)) -> Harness:
    store = AdmissionsStore(data_dir=tmp_path)
    store._save_whitelist_to_file(list(groups))
    reg = registry if registry is not None else FakeRegistry()
    # 延迟钳为 0：决策域测试不测随机 sleep（_delay_if_needed 另有专项用例）
    config = PluginConfig.from_dict(_ZERO_DELAY)
    return Harness(AdmissionService(reg, store, config), store, reg)


def make_req(**kw) -> JoinRequest:
    base = dict(group_openid="g1", join_request_id="req-1", member_openid="u1", comment="")
    base.update(kw)
    return JoinRequest(**base)


# --------------------------------------------------------------------------- #
# Decision 分支
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_approve_合法UID放行并写飞书(tmp_path):
    h = make_harness(tmp_path)
    req = make_req(comment="我的B站UID是 123456789")

    result = await h.service.admit_request(req)

    assert isinstance(result, AdmissionResult)
    assert result.decision == "approve"
    assert result.uid == "123456789"
    assert result.reason == ""
    assert h.registry.calls == [(123456789, "u1", "")]
    # 修复 #2 后：verified 标记保留（供 group_increase 跳过二次待补），pending 清空
    snap = h.store._snapshot_memory()
    assert "g1:u1" in snap["verified_before_join"]
    assert "g1:u1" not in snap["pending_uid"]


@pytest.mark.asyncio
async def test_decline_无UID拒绝且不写飞书(tmp_path):
    h = make_harness(tmp_path)
    req = make_req(comment="   ")

    result = await h.service.admit_request(req)

    assert result.decision == "decline"
    assert result.uid is None
    assert result.reason == "请在入群验证信息中提供B站UID"
    assert h.registry.calls == []


@pytest.mark.asyncio
async def test_decline_UID提取失败_格式不符(tmp_path):
    """纯文字无数字 / 数字不足 6 位均视为无有效 UID。"""
    h = make_harness(tmp_path)
    for comment in ("我爱这个群", "群号 123"):
        req = make_req(join_request_id=f"req-{comment}", comment=comment)
        result = await h.service.admit_request(req)
        assert result.decision == "decline", comment
    assert h.registry.calls == []


@pytest.mark.asyncio
async def test_whitelist_rejected_白名单外零动作(tmp_path):
    """非白名单群：不写飞书、不放行。dedup 消耗在白名单判断之前（顺序保持现状）。"""
    h = make_harness(tmp_path, groups=("g1",))
    req = make_req(group_openid="g-other", comment="123456789")

    result = await h.service.admit_request(req)

    assert result.decision == "decline"
    assert result.reason == "not_whitelisted"
    assert result.uid is None
    assert h.registry.calls == []
    snap = h.store._snapshot_memory()
    assert "g-other:u1" not in snap["verified_before_join"]


@pytest.mark.asyncio
async def test_duplicate_同join_request_id_首次完整流程_重复跳过(tmp_path):
    """修复 #1 后的正确语义：

    - 第 1 次：走完整审批流程（UID 校验 → 飞书 → approve）；
    - 第 2 次（同 join_request_id 重复申请）：decision="skip"，不再写飞书。
    """
    h = make_harness(tmp_path)
    first = make_req(comment="123456789")
    second = make_req(comment="123456789")  # 同 group:user:req-1

    r1 = await h.service.admit_request(first)
    r2 = await h.service.admit_request(second)

    assert r1.decision == "approve" and r1.uid == "123456789"
    assert r2.decision == "skip" and r2.reason == "duplicate"
    # 只有第一次写了飞书
    assert len(h.registry.calls) == 1


@pytest.mark.asyncio
async def test_feishu失败仍放行并入补偿队列(tmp_path):
    """飞书写入失败的原策略：仍放行用户，记录入 pending 由补偿任务重试。"""
    h = make_harness(tmp_path, registry=FakeRegistry(ok=False))
    req = make_req(comment="100200300")

    result = await h.service.admit_request(req)

    assert result.decision == "approve"
    assert result.uid == "100200300"
    assert result.reason == "feishu_pending"
    pending = h.store.list_pending()
    assert len(pending) == 1
    assert pending[0]["uid"] == "100200300"
    assert pending[0]["retry_count"] == 0
    assert pending[0]["group_id"] == "g1"


# --------------------------------------------------------------------------- #
# _extract_uid 纯函数
# --------------------------------------------------------------------------- #


def test_extract_uid_连续6位以上直接命中():
    assert _extract_uid("UID:123456789") == "123456789"
    assert _extract_uid("987654") == "987654"


def test_extract_uid_分段数字合并():
    assert _extract_uid("1 2 3 4 5 6") == "123456"
    assert _extract_uid("uid 123-456") == "123456"


def test_extract_uid_正则优先于合并():
    # 存在 >=6 位连续数字时取第一段，不再拼接后续散字
    assert _extract_uid("abc987654xyz 12") == "987654"


def test_extract_uid_不足6位返回None():
    assert _extract_uid("") is None
    assert _extract_uid("   ") is None
    assert _extract_uid(None) is None  # type: ignore[arg-type]
    assert _extract_uid("群号 12345") is None  # 合并后仅 5 位
    assert _extract_uid("hello world") is None


# --------------------------------------------------------------------------- #
# _delay_if_needed 边界（monkeypatch asyncio.sleep，不真等）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delay_min_max均为0时立即返回(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("astrbot_plugin_bili_verify_feishu.admission.asyncio.sleep", fake_sleep)
    config = PluginConfig.from_dict(_ZERO_DELAY)
    svc = AdmissionService(FakeRegistry(), AdmissionsStore(memory_only=True), config)
    await svc._delay_if_needed()
    assert slept == []


@pytest.mark.asyncio
async def test_delay_max小于min时钳到定值(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("astrbot_plugin_bili_verify_feishu.admission.asyncio.sleep", fake_sleep)
    # from_dict 会把 max 钳到 >= min；这里直接给 max < min 验证服务侧兜底
    config = PluginConfig.from_dict(
        {"REQUEST_DELAY_MIN_SECONDS": 3.0, "REQUEST_DELAY_MAX_SECONDS": 0}
    )
    assert config.delays.max_seconds == config.delays.min_seconds == 3.0
    svc = AdmissionService(FakeRegistry(), AdmissionsStore(memory_only=True), config)
    await svc._delay_if_needed()
    assert slept == [3.0]  # dmax == dmin → 固定延迟，无随机抖动
