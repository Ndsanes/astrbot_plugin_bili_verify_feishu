"""feishu_client 网关化改造的行为测试：注入假网关，断言端点映射与失败语义。

假网关记录每次调用的 method/path/params/data，并按预设队列返回响应；
不发任何真实子进程或网络调用。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from astrbot_plugin_bili_verify_feishu import feishu_client as fc

CONFIG: dict[str, Any] = {
    "FEISHU_APP_TOKEN": "appDemo",
    "FEISHU_TABLE_ID": "tblDemo",
    "MAX_RETRIES": 2,
    "RETRY_DELAY": 0,
}

RECORDS_BASE = "/open-apis/bitable/v1/apps/appDemo/tables/tblDemo/records"


class FakeGateway:
    """记录调用的假网关；responses 队列逐次弹出，error 时抛异常。"""

    def __init__(
        self,
        responses: list[Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses: list[Any] = list(responses or [])
        self._error = error

    async def api(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append(
            {"method": method, "path": path, "params": params, "data": data}
        )
        if self._error is not None:
            raise self._error
        if self._responses:
            return self._responses.pop(0)
        return {}

    async def send_text(self, target: str, text: str) -> None:  # pragma: no cover
        raise AssertionError("本套件只走 api 透传路径")


@pytest.fixture(autouse=True)
def _reset_gateway():
    """每个用例结束后解除网关注入，避免跨用例泄漏。"""
    yield
    fc.set_gateway(None)


# --------------------------------------------------------------------------- #
# 端点映射
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_搜索走records_search端点且过滤条件正确():
    gw = FakeGateway(responses=[{"items": [{"record_id": "recA1", "fields": {}}]}])
    fc.set_gateway(gw)

    ok, record_id = await fc._find_record_id_by_qq("123456", CONFIG)

    assert ok is True
    assert record_id == "recA1"
    assert len(gw.calls) == 1
    call = gw.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == f"{RECORDS_BASE}/search"
    assert call["params"] == {"page_size": 1}
    body = call["data"]
    assert body is not None
    assert body["field_names"] == ["QQ号"]
    cond = body["filter"]["conditions"][0]
    assert cond["field_name"] == "QQ号"
    assert cond["operator"] == "is"
    # 数字字符串 QQ 归一化为 int
    assert cond["value"] == [123456]


@pytest.mark.asyncio
async def test_新建记录走POST且fields为请求体():
    gw = FakeGateway(responses=[{"record": {"record_id": "recNew"}}])
    fc.set_gateway(gw)

    fields = {"UID": "100200300", "QQ号": 67890}
    ok = await fc.append_row_to_table(fields, CONFIG)

    assert ok is True
    call = gw.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == RECORDS_BASE
    assert call["data"] == {"fields": fields}


@pytest.mark.asyncio
async def test_upsert命中时走PUT更新对应record_id():
    gw = FakeGateway(
        responses=[
            {"items": [{"record_id": "recHit"}]},  # search 响应
            {"record": {"record_id": "recHit"}},  # update 响应
        ]
    )
    fc.set_gateway(gw)

    ok = await fc.upsert_member_row_by_qq({"状态": "在群"}, "openidABC", CONFIG)

    assert ok is True
    assert [c["method"] for c in gw.calls] == ["POST", "PUT"]
    update_call = gw.calls[1]
    assert update_call["path"] == f"{RECORDS_BASE}/recHit"
    assert update_call["data"] == {"fields": {"状态": "在群"}}


@pytest.mark.asyncio
async def test_upsert未命中时走POST新增():
    gw = FakeGateway(responses=[{"items": []}, {"record": {"record_id": "recAdd"}}])
    fc.set_gateway(gw)

    fields = {"UID": "42"}
    ok = await fc.upsert_member_row_by_qq(fields, 67890, CONFIG)

    assert ok is True
    assert [c["method"] for c in gw.calls] == ["POST", "POST"]
    assert gw.calls[1]["path"] == RECORDS_BASE
    assert gw.calls[1]["data"] == {"fields": fields}


@pytest.mark.asyncio
async def test_发消息走im_v1_messages且content为JSON包装():
    gw = FakeGateway(responses=[{"message_id": "omX"}])
    fc.set_gateway(gw)
    config = dict(CONFIG, OFFLINE_FEISHU_TARGETS=["ouTgt"], OFFLINE_FEISHU_ID_TYPE="open_id")

    sent = await fc.broadcast_feishu_message("hello", config)

    assert sent == 1
    call = gw.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/open-apis/im/v1/messages"
    assert call["params"] == {"receive_id_type": "open_id"}
    body = call["data"]
    assert isinstance(body, Mapping)
    assert body["receive_id"] == "ouTgt"
    assert body["msg_type"] == "text"
    import json

    assert json.loads(body["content"]) == {"text": "hello"}


@pytest.mark.asyncio
async def test_chat_id类型透传():
    gw = FakeGateway(responses=[{}])
    fc.set_gateway(gw)

    ok = await fc.send_feishu_message("hi", "ocChat", CONFIG, receive_id_type="chat_id")

    assert ok is True
    assert gw.calls[0]["params"] == {"receive_id_type": "chat_id"}


# --------------------------------------------------------------------------- #
# 失败语义与韧性
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_未注入网关时写入返回False不崩溃():
    fc.set_gateway(None)

    assert await fc.append_row_to_table({"UID": 1}, CONFIG) is False
    found = await fc._find_record_id_by_qq(123, CONFIG)
    assert found == (False, "")


@pytest.mark.asyncio
async def test_网关抛错时记ERROR并降级():
    fc.set_gateway(FakeGateway(error=RuntimeError("gateway boom")))

    assert await fc.append_row_to_table({"UID": 1}, CONFIG) is False
    assert await fc.update_member_status_by_qq(123, "在群", CONFIG) is False
    assert await fc.send_feishu_message("hi", "ouT", CONFIG) is False


@pytest.mark.asyncio
async def test_指数退避重试在第二次成功():
    # 首次调用网关返回 None（模拟失败），第二次成功
    gw = FakeGateway(responses=[None, {"record": {"record_id": "rec1"}}])
    fc.set_gateway(gw)

    ok = await fc.append_row_with_retry(
        {"UID": 7}, dict(CONFIG), max_retries=3, retry_delay=0
    )
    assert ok is True
    assert len(gw.calls) == 2


@pytest.mark.asyncio
async def test_app_token缺失直接失败不触网关():
    gw = FakeGateway()
    fc.set_gateway(gw)

    ok = await fc.append_row_to_table({"UID": 1}, {})

    assert ok is False
    assert gw.calls == []
