"""AdmissionService 决策域测试。

覆盖 admit_request / admit_message 的每个 Decision 分支与 _extract_uid 纯函数。
飞书经 FakeRegistry 替身注入，全程零网络。

⚠️ 已知疑似 bug（不在本任务修复，仅钉住现状）：
    admission.admit_request 中 ``if self._store.dedup(key):`` 的语义与
    AdmissionsStore.dedup 的 docstring 相反——dedup 首次见到某 key 返回 True
    （意为"新请求，可继续处理"），而 admit_request 把 True 当"重复命中"
    直接短路 approve(reason="dedup")。实际效果是：同一 join_request_id 的
    第一次申请不经过 UID 校验/白名单/飞书即被放行，重复的第二次申请反而
    走完整流程。相关测试以"先手动烧掉 dedup 首见"的方式绕过该短路来测
    后续分支，并在 duplicate 用例中断言现状。

    #2 admit_request 成功路径先 mark_verified 再 discard_pending，而
    AdmissionsStore.discard_pending 会同时清除 _verified_before_join——
    mark_verified "供 group_increase 跳过二次待补"的效果被立即抹掉。
    test_approve_合法UID放行并写飞书 已钉住该现状。
"""

from __future__ import annotations

import json
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

GROUP = "6CCC18AB28098F241B44FF1A41F6668F"


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


class FakeRegistry:
    """MemberRegistry 替身：记录调用、可控制成败。"""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[tuple[int, str, str]] = []

    async def register(self, uid: int, openid: str, nickname: str = "") -> bool:
        self.calls.append((uid, openid, nickname))
        return self.ok


@dataclass(slots=True)
class Harness:
    service: AdmissionService
    store: AdmissionsStore
    registry: FakeRegistry


_ZERO_DELAY = {"REQUEST_DELAY_MIN_SECONDS": 0, "REQUEST_DELAY_MAX_SECONDS": 0}


def make_harness(tmp_path, registry: FakeRegistry | None = None, groups=("g1",)) -> Harness:
    store = AdmissionsStore(data_dir=tmp_path)
    # 白名单文件由测试直接写入（store 只负责读）
    (tmp_path / "whitelist.json").write_text(
        json.dumps({"groups": list(groups)}, ensure_ascii=False), encoding="utf-8"
    )
    reg = registry or FakeRegistry()
    # 延迟钳为 0：决策域测试不测随机 sleep（_delay_if_needed 另有专项用例）
    config = PluginConfig.from_dict(_ZERO_DELAY)
    return Harness(AdmissionService(reg, store, config), store, reg)


def make_req(**kw) -> JoinRequest:
    base = dict(group_openid="g1", join_request_id="req-1", member_openid="u1", comment="")
    base.update(kw)
    return JoinRequest(**base)


def burn_dedup(h: Harness, req: JoinRequest) -> None:
    """绕过 dedup 反转短路（见模块 docstring 疑似 bug）：公开 API 手动消耗首见。"""
    h.store.dedup(f"{req.group_openid.strip()}:{req.member_openid.strip()}:{req.join_request_id}")


# --------------------------------------------------------------------------- #
# Decision 分支
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_approve_合法UID放行并写飞书(tmp_path):
    h = make_harness(tmp_path)
    req = make_req(comment="我的B站UID是 123456789")
    burn_dedup(h, req)

    result = await h.service.admit_request(req)

    assert isinstance(result, AdmissionResult)
    assert result.decision == "approve"
    assert result.uid == "123456789"
    assert result.reason == ""
    assert h.registry.calls == [(123456789, "u1", "")]
    # ⚠️ 现状（疑似 bug #2）：admit_request 成功后先 mark_verified 再 discard_pending，
    # 而 discard_pending 会同时清除 verified 集合——mark_verified 的
    # "供 group_increase 跳过二次待补"效果被立即抹掉。此处钉住现状。
    snap = h.store._snapshot_memory()
    assert "g1:u1" not in snap["verified_before_join"]
    assert "g1:u1" not in snap["pending_uid"]


@pytest.mark.asyncio
async def test_decline_无UID拒绝且不写飞书(tmp_path):
    h = make_harness(tmp_path)
    req = make_req(comment="   ")
    burn_dedup(h, req)

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
        burn_dedup(h, req)
        result = await h.service.admit_request(req)
        assert result.decision == "decline", comment
    assert h.registry.calls == []


@pytest.mark.asyncio
async def test_whitelist_rejected_白名单外零动作(tmp_path):
    """非白名单群：不写飞书、不放行。注意 dedup 消耗在白名单判断之前（现状）。"""
    h = make_harness(tmp_path, groups=("g1",))
    req = make_req(group_openid="g-other", comment="123456789")
    burn_dedup(h, req)

    result = await h.service.admit_request(req)

    assert result.decision == "decline"
    assert result.reason == "not_whitelisted"
    assert result.uid is None
    assert h.registry.calls == []
    snap = h.store._snapshot_memory()
    assert "g-other:u1" not in snap["verified_before_join"]


@pytest.mark.asyncio
async def test_duplicate_同join_request_id去重_现状断言(tmp_path):
    """⚠️ 断言的是疑似反转后的现状（见模块 docstring）：

    - 第 1 次：dedup 首见 True → 被当"重复"短路 approve(uid=None, reason="dedup")，
      未做任何 UID 校验、未写飞书；
    - 第 2 次（同 join_request_id 重复申请）：dedup False → 反而走完整审批流程。
      若上游修复反转语义，本用例应同步改写。
    """
    h = make_harness(tmp_path)
    first = make_req(comment="123456789")
    second = make_req(comment="123456789")  # 同 group:user:req-1

    r1 = await h.service.admit_request(first)
    r2 = await h.service.admit_request(second)

    assert r1.decision == "approve" and r1.reason == "dedup" and r1.uid is None
    assert r2.decision == "approve" and r2.uid == "123456789" and r2.reason == ""
    # 第一次短路未写飞书；第二次才走完整流程写了一次
    assert len(h.registry.calls) == 1


@pytest.mark.asyncio
async def test_feishu失败仍放行并入补偿队列(tmp_path):
    """飞书写入失败的原策略：仍放行用户，记录入 pending 由补偿任务重试。"""
    h = make_harness(tmp_path, registry=FakeRegistry(ok=False))
    req = make_req(comment="100200300")
    burn_dedup(h, req)

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
# admit_message — 群消息补录路径
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_admit_message_补录成功并discard_pending(tmp_path):
    h = make_harness(tmp_path)
    h.store.remember_pending("g1", "u1")

    ok = await h.service.admit_message("g1", "u1", "UID 555000111", nickname="小明")

    assert ok is True
    assert h.registry.calls == [(555000111, "u1", "小明")]
    assert not h.store.is_pending("g1", "u1")


@pytest.mark.asyncio
async def test_admit_message_白名单外返回False(tmp_path):
    h = make_harness(tmp_path, groups=("g1",))
    assert await h.service.admit_message("g-x", "u1", "123456789") is False
    assert await h.service.admit_message("", "u1", "123456789") is False
    assert h.registry.calls == []


@pytest.mark.asyncio
async def test_admit_message_无UID返回False(tmp_path):
    h = make_harness(tmp_path)
    assert await h.service.admit_message("g1", "u1", "不知道写啥") is False
    assert h.registry.calls == []


@pytest.mark.asyncio
async def test_admit_message_飞书失败入队并返回False(tmp_path):
    h = make_harness(tmp_path, registry=FakeRegistry(ok=False))
    ok = await h.service.admit_message("g1", "u1", "999888777")
    assert ok is False
    assert [r["uid"] for r in h.store.list_pending()] == ["999888777"]


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
