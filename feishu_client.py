"""feishu_client — 通过 lark_cli 平台网关调用飞书开放接口的薄客户端。

认证、登录态、TAT 刷新与限速全部由 lark_cli 平台适配器（网关）单点负责；
本模块只保留业务语义：多维表格记录读写与消息发送，以及重试/指数退避韧性。
main.py 在 initialize 时通过 set_gateway() 注入网关实例；未注入或网关调用
抛错时按既有失败语义返回 False / 空串，不抛出、不崩溃。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from astrbot.api import logger
except Exception:
    logger = logging.getLogger(__name__)

# 模块级网关实例（由 main.initialize 注入；None 表示未接入网关）
_gateway: Any | None = None


def set_gateway(gateway: Any) -> None:
    """注入 lark_cli 平台适配器（网关）实例；传 None 表示解除注入。"""
    global _gateway
    _gateway = gateway


def _get_gateway() -> Any | None:
    """返回已注入的网关实例；未注入时返回 None（调用方走降级路径）。"""
    return _gateway


async def _gateway_api(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """经网关透传一次飞书开放 API 调用，失败时记 ERROR 并返回 None。"""
    gateway = _get_gateway()
    if gateway is None:
        logger.error(f"lark_cli 网关未注入，放弃飞书调用: {method} {path}")
        return None
    try:
        result: Any = await gateway.api(method, path, params=params, data=data)
    except Exception as e:
        logger.error(f"lark_cli 网关调用异常 ({method} {path}): {e}")
        return None
    if not isinstance(result, Mapping):
        logger.error(f"lark_cli 网关返回非 dict 响应 ({method} {path})")
        return None
    return dict(result)


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


def _records_base_path(app_token: str, table_id: str) -> str:
    """拼出多维表格 records 集合的 API 基础路径。"""
    return f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"


def _normalize_qq_value(qq_num: int | str) -> int | str:
    """兼容 openid 字符串：数字 QQ 转 int，其余原样透传。"""
    if isinstance(qq_num, str) and qq_num.isdigit():
        try:
            return int(qq_num)
        except ValueError:
            return qq_num
    return qq_num


async def append_row_to_table(
    fields: dict,
    config: Mapping[str, Any],
    app_token: str | None = None,
    table_id: str | None = None,
) -> bool:
    """向飞书多维表格追加一行记录。

    Args:
        fields: 要写入的字段数据，格式如 {"UID": "12345", "QQ号": "67890"}
        config: 插件配置映射
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

    if not app_token or not table_id:
        logger.error("飞书写入失败: FEISHU_APP_TOKEN 或 FEISHU_TABLE_ID 未配置")
        return False

    result = await _gateway_api(
        "POST",
        _records_base_path(app_token, table_id),
        data={"fields": fields},
    )
    if result is None:
        return False

    record_id = ""
    record = result.get("record")
    if isinstance(record, Mapping):
        record_id = str(record.get("record_id", "") or "")
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
        config: 插件配置映射
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
            logger.warning(f"飞书写入失败，{delay:.1f}秒后进行第 {attempt + 2} 次重试...")
            await asyncio.sleep(delay)

    logger.error(f"飞书写入失败，已重试 {_max_retries} 次")
    return False


def _first_record_id_from_search_response(result: Mapping[str, Any] | None) -> str:
    """从搜索响应 data 中提取首条记录 ID。"""
    if not isinstance(result, Mapping):
        return ""
    items = result.get("items")
    if not isinstance(items, list) or not items:
        return ""

    first_item = items[0]
    if isinstance(first_item, Mapping):
        return str(first_item.get("record_id", "") or "").strip()
    return ""


async def _find_record_id_by_qq(
    qq_num: int | str,
    config: Mapping[str, Any],
    qq_field_name: str = "QQ号",
) -> tuple[bool, str]:
    """按 QQ 查询首条记录，返回(查询成功, record_id)。兼容数字 QQ 与 openid 字符串。"""
    app_token, table_id = _normalize_bitable_ids(
        config.get("FEISHU_APP_TOKEN", ""),
        config.get("FEISHU_TABLE_ID", ""),
    )
    if not app_token or not table_id:
        logger.error("飞书查询失败: FEISHU_APP_TOKEN 或 FEISHU_TABLE_ID 未配置")
        return False, ""

    qq_field = str(qq_field_name).strip() or "QQ号"
    filter_payload = {
        "conjunction": "and",
        "conditions": [
            {
                "field_name": qq_field,
                "operator": "is",
                "value": [_normalize_qq_value(qq_num)],
            }
        ],
    }

    result = await _gateway_api(
        "POST",
        f"{_records_base_path(app_token, table_id)}/search",
        params={"page_size": 1},
        data={"field_names": [qq_field], "filter": filter_payload},
    )
    if result is None:
        return False, ""

    return True, _first_record_id_from_search_response(result)


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

    result = await _gateway_api(
        "PUT",
        f"{_records_base_path(app_token, table_id)}/{record_id}",
        data={"fields": fields},
    )
    if result is None:
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

    result = await _gateway_api(
        "PUT",
        f"{_records_base_path(app_token, table_id)}/{record_id}",
        data={"fields": {status_field: status_text}},
    )
    if result is None:
        return False

    logger.info(
        f"飞书状态更新成功: QQ={qq_num}, 状态={status_text}, record_id={record_id}"
    )
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
    """通过网关发送私聊/群聊消息（POST /open-apis/im/v1/messages）。

    文档: https://open.feishu.cn/document/server-docs/im-v1/message/create
    认证与限速由 lark_cli 平台网关负责；本函数只组装业务报文。
    """
    receive_id = str(receive_id or "").strip()
    receive_id_type = str(receive_id_type or "open_id").strip() or "open_id"
    if not receive_id:
        logger.warning("[FeishuMessage] 发送跳过: receive_id 为空")
        return False

    # 允许的 receive_id_type: open_id, user_id, union_id, email, chat_id
    if receive_id_type not in {"open_id", "user_id", "union_id", "email", "chat_id"}:
        logger.warning(
            f"[FeishuMessage] 未知的 receive_id_type={receive_id_type}，回退为 open_id"
        )
        receive_id_type = "open_id"

    # 内容包装：text 类型需为 JSON 字符串 {"text": "..."}
    if msg_type == "text":
        text_content = json.dumps({"text": str(content)}, ensure_ascii=False)
    else:
        text_content = str(content)

    result = await _gateway_api(
        "POST",
        "/open-apis/im/v1/messages",
        params={"receive_id_type": receive_id_type},
        data={
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": text_content,
        },
    )
    if result is None:
        return False

    logger.info(
        f"[FeishuMessage] 发送成功 receive_id={receive_id}, type={receive_id_type}"
    )
    return True


async def broadcast_feishu_message(
    content: str,
    config: Mapping[str, Any],
) -> int:
    """向 OFFLINE_FEISHU_TARGETS 配置的所有目标广播同一条消息。返回成功数。"""
    raw_targets = config.get("OFFLINE_FEISHU_TARGETS", [])
    id_type = (
        str(config.get("OFFLINE_FEISHU_ID_TYPE", "open_id") or "open_id").strip()
        or "open_id"
    )

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
