import asyncio
import logging
import time
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    CreateAppTableRecordRequest,
    CreateAppTableRecordResponse,
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
