import asyncio
import logging
import time
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    CreateAppTableRecordRequest,
    CreateAppTableRecordResponse,
    SearchAppTableRecordRequest,
    SearchAppTableRecordRequestBody,
    SearchAppTableRecordResponse,
    UpdateAppTableRecordRequest,
    UpdateAppTableRecordResponse,
)

try:
    # 旧版 SDK
    from lark_oapi.api.bitable.v1 import CreateAppTableRecordRequestBody as RecordBody
except ImportError:
    # 新版 SDK 使用 AppTableRecord 作为请求体
    from lark_oapi.api.bitable.v1 import AppTableRecord as RecordBody

try:
    from astrbot.api import logger
except Exception:
    logger = logging.getLogger(__name__)

# 全局客户端实例（延迟初始化）
_client: lark.Client | None = None
_client_key: tuple[str, str] | None = None

# 飞书 API 限速：统一限制为每秒最多 5 次请求
MAX_REQUESTS_PER_SECOND = 5
_MIN_REQUEST_INTERVAL = 1.0 / MAX_REQUESTS_PER_SECOND
_rate_limit_lock = asyncio.Lock()
_next_request_ts = 0.0


def _safe_int(value: Any, default: int, minimum: int = 1) -> int:
    """安全解析整型配置，异常时回退默认值。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, minimum)


def _safe_float(value: Any, default: float, minimum: float = 0.0) -> float:
    """安全解析浮点配置，异常时回退默认值。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, minimum)


def _strip_query_like_suffix(value: str) -> str:
    """移除误粘贴的查询参数或锚点内容。"""
    cleaned = value.strip()
    for sep in ("&", "?", "#"):
        if sep in cleaned:
            cleaned = cleaned.split(sep, 1)[0]
    return cleaned.strip()


def _normalize_bitable_ids(
    app_token_raw: Any,
    table_id_raw: Any,
) -> tuple[str, str]:
    """归一化 app_token/table_id，兼容粘贴完整多维表格 URL。"""
    app_token = str(app_token_raw or "").strip()
    table_id = str(table_id_raw or "").strip()

    url_source = ""
    if "://" in app_token:
        url_source = app_token
    elif "://" in table_id:
        url_source = table_id

    if url_source:
        parsed = urlparse(url_source)
        path_parts = [part for part in parsed.path.split("/") if part]

        # 常见多维表格 URL: https://xxx.feishu.cn/base/<app_token>?table=<table_id>
        if "base" in path_parts:
            base_idx = path_parts.index("base")
            if base_idx + 1 < len(path_parts):
                app_token = path_parts[base_idx + 1]

        query = parse_qs(parsed.query)
        table_values = query.get("table")
        if table_values and table_values[0]:
            table_id = table_values[0]

    app_token = _strip_query_like_suffix(app_token)
    table_id = _strip_query_like_suffix(table_id)
    return app_token, table_id


async def _acquire_rate_limit_slot() -> None:
    """在单进程内按固定间隔发起请求，避免触发飞书限速。"""
    global _next_request_ts

    async with _rate_limit_lock:
        now = time.monotonic()
        if now < _next_request_ts:
            await asyncio.sleep(_next_request_ts - now)
            now = time.monotonic()
        _next_request_ts = now + _MIN_REQUEST_INTERVAL


def _get_client(config: Mapping[str, Any]) -> lark.Client:
    """获取或创建飞书 API 客户端实例。"""
    global _client, _client_key

    app_id = str(config.get("FEISHU_APP_ID", "")).strip()
    app_secret = str(config.get("FEISHU_APP_SECRET", "")).strip()
    key = (app_id, app_secret)
    if _client is None or _client_key != key:
        _client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )
        _client_key = key
        logger.info("飞书 API 客户端已初始化")
    return _client


async def append_row_to_table(
    fields: dict,
    config: Mapping[str, Any],
    app_token: str | None = None,
    table_id: str | None = None,
) -> bool:
    """向飞书多维表格追加一行记录。

    Args:
        fields: 要写入的字段数据，格式如 {"UID": "12345", "QQ号": "67890"}
        app_token: 多维表格 app_token，默认从配置读取
        table_id: 数据表 table_id，默认从配置读取

    Returns:
        写入成功返回 True，失败返回 False
    """
    raw_app_token = app_token
    raw_table_id = table_id
    if app_token is None:
        raw_app_token = config.get("FEISHU_APP_TOKEN", "")
    if table_id is None:
        raw_table_id = config.get("FEISHU_TABLE_ID", "")

    app_token, table_id = _normalize_bitable_ids(raw_app_token, raw_table_id)

    if str(raw_table_id or "").strip() != table_id:
        logger.warning("检测到 FEISHU_TABLE_ID 含多余参数，已自动清洗后再写入")
    if str(raw_app_token or "").strip() != app_token:
        logger.warning("检测到 FEISHU_APP_TOKEN 格式异常，已自动清洗后再写入")

    client = _get_client(config)

    if not app_token or not table_id:
        logger.error("飞书写入失败: FEISHU_APP_TOKEN 或 FEISHU_TABLE_ID 未配置")
        return False

    # 构建请求对象
    request: CreateAppTableRecordRequest = (
        CreateAppTableRecordRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .request_body(RecordBody.builder().fields(fields).build())
        .build()
    )

    # 发起异步请求
    try:
        await _acquire_rate_limit_slot()
        response: CreateAppTableRecordResponse = (
            await client.bitable.v1.app_table_record.acreate(request)
        )
    except Exception as e:
        logger.error(f"飞书写入异常: {e}")
        return False

    # 处理响应
    if not response.success():
        logger.error(
            f"飞书写入失败, code: {response.code}, msg: {response.msg}, "
            f"log_id: {response.get_log_id()}"
        )
        return False

    record_id = getattr(getattr(response.data, "record", None), "record_id", "")
    logger.info(f"飞书写入成功, record_id: {record_id}")
    return True


async def append_row_with_retry(
    fields: dict,
    config: Mapping[str, Any],
    max_retries: int | None = None,
    retry_delay: float | None = None,
) -> bool:
    """带重试机制的写入操作（指数退避）。

    Args:
        fields: 要写入的字段数据
        max_retries: 最大重试次数，默认从配置读取
        retry_delay: 基础重试延迟（秒），默认从配置读取

    Returns:
        最终写入成功返回 True，所有重试均失败返回 False
    """
    _max_retries: int = _safe_int(
        max_retries if max_retries is not None else config.get("MAX_RETRIES", 3),
        default=3,
        minimum=1,
    )
    _retry_delay: float = _safe_float(
        retry_delay if retry_delay is not None else config.get("RETRY_DELAY", 1),
        default=1.0,
        minimum=0.0,
    )

    for attempt in range(_max_retries):
        success = await append_row_to_table(fields, config)
        if success:
            return True

        if attempt < _max_retries - 1:
            delay = _retry_delay * (2**attempt)
            logger.warning(
                f"飞书写入失败，{delay:.1f}秒后进行第 {attempt + 2} 次重试..."
            )
            await asyncio.sleep(delay)

    logger.error(f"飞书写入失败，已重试 {_max_retries} 次")
    return False


def _first_record_id_from_search_response(response: SearchAppTableRecordResponse) -> str:
    """从搜索响应中提取首条记录 ID。"""
    data = getattr(response, "data", None)
    items = getattr(data, "items", None)
    if not isinstance(items, list) or not items:
        return ""

    first_item = items[0]
    record_id = getattr(first_item, "record_id", "")
    if record_id:
        return str(record_id).strip()

    if isinstance(first_item, dict):
        return str(first_item.get("record_id", "")).strip()

    return ""


async def _find_record_id_by_qq(
    qq_num: int | str,
    config: Mapping[str, Any],
    qq_field_name: str = "QQ号",
) -> tuple[bool, str]:
    """按 QQ 查询首条记录，返回(查询成功, record_id)。兼容数字 QQ 与 qq_official 的 openid 字符串。"""
    app_token, table_id = _normalize_bitable_ids(
        config.get("FEISHU_APP_TOKEN", ""),
        config.get("FEISHU_TABLE_ID", ""),
    )
    if not app_token or not table_id:
        logger.error("飞书查询失败: FEISHU_APP_TOKEN 或 FEISHU_TABLE_ID 未配置")
        return False, ""

    qq_field = str(qq_field_name).strip() or "QQ号"
    client = _get_client(config)
    # 兼容 openid 字符串：飞书侧字段若为文本类型需传字符串，若为数字类型则传 int
    qq_value: int | str = qq_num
    if isinstance(qq_num, str) and qq_num.isdigit():
        try:
            qq_value = int(qq_num)
        except ValueError:
            qq_value = qq_num
    filter_payload = {
        "conjunction": "and",
        "conditions": [
            {
                "field_name": qq_field,
                "operator": "is",
                "value": [qq_value],
            }
        ],
    }

    search_request: SearchAppTableRecordRequest = (
        SearchAppTableRecordRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .page_size(1)
        .request_body(
            SearchAppTableRecordRequestBody.builder()
            .filter(filter_payload)
            .build()
        )
        .build()
    )

    try:
        await _acquire_rate_limit_slot()
        search_response: SearchAppTableRecordResponse = (
            await client.bitable.v1.app_table_record.asearch(search_request)
        )
    except Exception as e:
        logger.error(f"飞书查询记录异常: {e}")
        return False, ""

    if not search_response.success():
        logger.error(
            "飞书查询记录失败, "
            f"code: {search_response.code}, msg: {search_response.msg}, "
            f"log_id: {search_response.get_log_id()}"
        )
        return False, ""

    record_id = _first_record_id_from_search_response(search_response)
    return True, record_id


async def upsert_member_row_by_qq(
    fields: dict,
    qq_num: int | str,
    config: Mapping[str, Any],
    qq_field_name: str = "QQ号",
) -> bool:
    """按 QQ 先查后写：命中则更新，未命中则新增。兼容 openid 字符串。"""
    found_ok, record_id = await _find_record_id_by_qq(
        qq_num=qq_num,
        config=config,
        qq_field_name=qq_field_name,
    )
    if not found_ok:
        return False

    if not record_id:
        return await append_row_to_table(fields, config)

    app_token, table_id = _normalize_bitable_ids(
        config.get("FEISHU_APP_TOKEN", ""),
        config.get("FEISHU_TABLE_ID", ""),
    )
    if not app_token or not table_id:
        logger.error("飞书更新失败: FEISHU_APP_TOKEN 或 FEISHU_TABLE_ID 未配置")
        return False

    client = _get_client(config)
    update_request: UpdateAppTableRecordRequest = (
        UpdateAppTableRecordRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .record_id(record_id)
        .request_body(RecordBody.builder().fields(fields).build())
        .build()
    )

    try:
        await _acquire_rate_limit_slot()
        update_response: UpdateAppTableRecordResponse = (
            await client.bitable.v1.app_table_record.aupdate(update_request)
        )
    except Exception as e:
        logger.error(f"飞书更新记录异常: {e}")
        return False

    if not update_response.success():
        logger.error(
            "飞书更新记录失败, "
            f"code: {update_response.code}, msg: {update_response.msg}, "
            f"log_id: {update_response.get_log_id()}"
        )
        return False

    logger.info(f"飞书记录已按QQ复用更新: QQ={qq_num}, record_id={record_id}")
    return True


async def upsert_member_row_by_qq_with_retry(
    fields: dict,
    qq_num: int | str,
    config: Mapping[str, Any],
    qq_field_name: str = "QQ号",
    max_retries: int | None = None,
    retry_delay: float | None = None,
) -> bool:
    """按 QQ 先查后写，失败时指数退避重试。兼容 openid。"""
    _max_retries: int = _safe_int(
        max_retries if max_retries is not None else config.get("MAX_RETRIES", 3),
        default=3,
        minimum=1,
    )
    _retry_delay: float = _safe_float(
        retry_delay if retry_delay is not None else config.get("RETRY_DELAY", 1),
        default=1.0,
        minimum=0.0,
    )

    for attempt in range(_max_retries):
        success = await upsert_member_row_by_qq(
            fields=fields,
            qq_num=qq_num,
            config=config,
            qq_field_name=qq_field_name,
        )
        if success:
            return True

        if attempt < _max_retries - 1:
            delay = _retry_delay * (2**attempt)
            logger.warning(
                f"飞书按QQ复用写入失败，{delay:.1f}秒后进行第 {attempt + 2} 次重试..."
            )
            await asyncio.sleep(delay)

    logger.error(f"飞书按QQ复用写入失败，已重试 {_max_retries} 次, QQ={qq_num}")
    return False


async def update_member_status_by_qq(
    qq_num: int | str,
    status_value: str,
    config: Mapping[str, Any],
    qq_field_name: str = "QQ号",
    status_field_name: str = "状态",
) -> bool:
    """按 QQ 号查找并更新成员状态字段。兼容 openid。"""
    found_ok, record_id = await _find_record_id_by_qq(
        qq_num=qq_num,
        config=config,
        qq_field_name=qq_field_name,
    )
    if not found_ok:
        logger.error(f"飞书状态更新失败: 查询QQ对应记录失败, QQ={qq_num}")
        return False

    status_text = str(status_value).strip()
    status_field = str(status_field_name).strip() or "状态"
    if not record_id:
        logger.warning(f"飞书状态更新跳过: 未找到QQ对应记录, QQ={qq_num}")
        return False

    app_token, table_id = _normalize_bitable_ids(
        config.get("FEISHU_APP_TOKEN", ""),
        config.get("FEISHU_TABLE_ID", ""),
    )
    if not app_token or not table_id:
        logger.error("飞书状态更新失败: FEISHU_APP_TOKEN 或 FEISHU_TABLE_ID 未配置")
        return False

    client = _get_client(config)

    update_request: UpdateAppTableRecordRequest = (
        UpdateAppTableRecordRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .record_id(record_id)
        .request_body(RecordBody.builder().fields({status_field: status_text}).build())
        .build()
    )

    try:
        await _acquire_rate_limit_slot()
        update_response: UpdateAppTableRecordResponse = (
            await client.bitable.v1.app_table_record.aupdate(update_request)
        )
    except Exception as e:
        logger.error(f"飞书状态更新失败，更新记录异常: {e}")
        return False

    if not update_response.success():
        logger.error(
            "飞书状态更新失败，更新记录失败, "
            f"code: {update_response.code}, msg: {update_response.msg}, "
            f"log_id: {update_response.get_log_id()}"
        )
        return False

    logger.info(f"飞书状态更新成功: QQ={qq_num}, 状态={status_text}, record_id={record_id}")
    return True


async def update_member_status_by_qq_with_retry(
    qq_num: int | str,
    status_value: str,
    config: Mapping[str, Any],
    qq_field_name: str = "QQ号",
    status_field_name: str = "状态",
    max_retries: int | None = None,
    retry_delay: float | None = None,
) -> bool:
    """按 QQ 更新成员状态，失败时指数退避重试。兼容 openid。"""
    _max_retries: int = _safe_int(
        max_retries if max_retries is not None else config.get("MAX_RETRIES", 3),
        default=3,
        minimum=1,
    )
    _retry_delay: float = _safe_float(
        retry_delay if retry_delay is not None else config.get("RETRY_DELAY", 1),
        default=1.0,
        minimum=0.0,
    )

    for attempt in range(_max_retries):
        success = await update_member_status_by_qq(
            qq_num=qq_num,
            status_value=status_value,
            config=config,
            qq_field_name=qq_field_name,
            status_field_name=status_field_name,
        )
        if success:
            return True

        if attempt < _max_retries - 1:
            delay = _retry_delay * (2**attempt)
            logger.warning(
                f"飞书状态更新失败，{delay:.1f}秒后进行第 {attempt + 2} 次重试..."
            )
            await asyncio.sleep(delay)
    logger.error(f"飞书状态更新失败，已重试 {_max_retries} 次, QQ={qq_num}")
    return False


async def send_feishu_message(
    content: str,
    receive_id: str,
    config: Mapping[str, Any],
    receive_id_type: str = "open_id",
    msg_type: str = "text",
) -> bool:
    """通过飞书开放 API (im/v1/messages) 发送私聊/群聊消息。

    文档: https://open.feishu.cn/document/server-docs/im-v1/message/create
    SDK: lark_oapi.api.im.v1.CreateMessageRequest / CreateMessageResponse
    权限需求: im:message:send_as_bot (或 im:message)，且机器人与目标有会话。
    """
    import json as _json

    receive_id = str(receive_id or "").strip()
    receive_id_type = str(receive_id_type or "open_id").strip() or "open_id"
    if not receive_id:
        logger.warning("[FeishuMessage] 发送跳过: receive_id 为空")
        return False

    # 允许的 receive_id_type: open_id, user_id, union_id, email, chat_id
    if receive_id_type not in {"open_id", "user_id", "union_id", "email", "chat_id"}:
        logger.warning(f"[FeishuMessage] 未知的 receive_id_type={receive_id_type}，回退为 open_id")
        receive_id_type = "open_id"

    # 内容包装：text 类型需为 JSON 字符串 {"text": "..."}
    if msg_type == "text":
        text_content = _json.dumps({"text": str(content)}, ensure_ascii=False)
    else:
        text_content = str(content)

    # 校验基础配置
    app_id = str(config.get("FEISHU_APP_ID", "") or "").strip()
    app_secret = str(config.get("FEISHU_APP_SECRET", "") or "").strip()
    if not app_id or not app_secret:
        logger.error("[FeishuMessage] 发送失败: FEISHU_APP_ID / FEISHU_APP_SECRET 未配置")
        return False

    client = _get_client(config)

    try:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    except ImportError as e:
        logger.error(f"[FeishuMessage] 导入飞书消息模型失败: {e}")
        return False

    request = (
        CreateMessageRequest.builder()
        .receive_id_type(receive_id_type)
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type(msg_type)
            .content(text_content)
            .build()
        )
        .build()
    )

    try:
        await _acquire_rate_limit_slot()
        # lark SDK 异步方法为 acreate
        response = await client.im.v1.message.acreate(request)
    except Exception as e:
        logger.error(f"[FeishuMessage] 发送异常 receive_id={receive_id}: {e}")
        return False

    if not response.success():
        logger.error(
            f"[FeishuMessage] 发送失败 receive_id={receive_id}, "
            f"code={response.code}, msg={response.msg}, log_id={response.get_log_id()}"
        )
        return False

    logger.info(f"[FeishuMessage] 发送成功 receive_id={receive_id}, type={receive_id_type}")
    return True


async def broadcast_feishu_message(
    content: str,
    config: Mapping[str, Any],
) -> int:
    """向 OFFLINE_FEISHU_TARGETS 配置的所有目标广播同一条消息。返回成功数。"""
    raw_targets = config.get("OFFLINE_FEISHU_TARGETS", [])
    id_type = str(config.get("OFFLINE_FEISHU_ID_TYPE", "open_id") or "open_id").strip() or "open_id"

    targets: list[str] = []
    if isinstance(raw_targets, list):
        targets = [str(t).strip() for t in raw_targets if str(t).strip()]
    elif isinstance(raw_targets, str) and raw_targets.strip():
        targets = [p.strip() for p in raw_targets.split(",") if p.strip()]

    if not targets:
        logger.warning("[FeishuMessage] 未配置 OFFLINE_FEISHU_TARGETS，跳过飞书通知")
        return 0

    success = 0
    for tid in targets:
        ok = await send_feishu_message(
            content=content,
            receive_id=tid,
            config=config,
            receive_id_type=id_type,
        )
        if ok:
            success += 1
    return success
