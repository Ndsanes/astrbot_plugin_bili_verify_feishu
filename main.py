import asyncio
import random
import re
from datetime import datetime, timezone
from typing import Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger

from .feishu_client import (
    broadcast_feishu_message,
    upsert_member_row_by_qq_with_retry,
    update_member_status_by_qq_with_retry,
)
from .storage import (
    is_group_whitelisted,
    load_whitelist,
    load_pending,
    save_whitelist,
    add_to_pending,
)


@register(
    "bili_verify_feishu",
    "NDsans",
    "QQ入群请求自动登记B站UID到飞书多维表格",
    "0.0.2",
    "https://github.com/Ndsanes/astrbot_plugin_bili_verify_feishu",
)
class BiliVerifyFeishuPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._uid_pattern = re.compile(
            r"(?:b站|bilibili|uid|UID)[：:\s]*(\d{4,})|^(\\d{6,})$"
        )
        # 已入群但尚未提供 UID 的用户集合，格式: "{group_id}:{user_id}"
        self._pending_uid: set[str] = set()
        # 已在入群请求阶段完成 UID 校验，等待 group_increase 落地的用户。
        self._verified_before_join: set[str] = set()
        self._processed_request_keys: set[str] = set()
        self._pending_check_task: asyncio.Task | None = None
        # QQ 掉线检测（通过飞书通知）
        self._offline_monitor_task: asyncio.Task | None = None
        self._offline_fail_count: int = 0
        self._offline_is_down: bool = False

    def _get_config(self, key: str, default: Any = None) -> Any:
        """读取 AstrBot 注入的插件配置。"""
        return self.config.get(key, default)

    def _safe_int(self, value: Any, default: int, minimum: int = 1) -> int:
        """安全解析整数配置，异常时回退默认值。"""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return max(parsed, minimum)

    def _safe_bool(self, value: Any, default: bool = True) -> bool:
        """安全解析布尔配置，异常时回退默认值。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        return default

    def _safe_float(self, value: Any, default: float, minimum: float = 0.0) -> float:
        """安全解析浮点配置，异常时回退默认值。"""
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return max(parsed, minimum)

    def _validate_config(self) -> list[str]:
        """校验必填配置项是否完整。"""
        errors: list[str] = []
        required = [
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
            "FEISHU_APP_TOKEN",
            "FEISHU_TABLE_ID",
        ]
        for key in required:
            value = self._get_config(key, "")
            if not isinstance(value, str) or not value.strip():
                errors.append(f"缺少必要配置: {key}")
        return errors

    def _init_whitelist_from_config(self) -> None:
        """首次启动时可从配置初始化白名单，不覆盖已持久化数据。"""
        existing = load_whitelist()
        if existing:
            return

        raw_groups = self._get_config("WHITELIST_GROUPS", [])
        groups: list[str] = []
        if isinstance(raw_groups, list):
            groups = [str(g).strip() for g in raw_groups if str(g).strip()]
        elif isinstance(raw_groups, str):
            # 兼容将白名单写成逗号分隔字符串的场景。
            groups = [g.strip() for g in raw_groups.split(",") if g.strip()]

        if groups:
            save_whitelist(groups)
            logger.info(f"[BiliVerifyFeishu] 已从插件配置初始化白名单，群数: {len(groups)}")

    async def initialize(self):
        """插件初始化，加载配置并校验。"""
        self._init_whitelist_from_config()
        errors = self._validate_config()
        if errors:
            for err in errors:
                logger.warning(f"[BiliVerifyFeishu] 配置问题: {err}")
        whitelist = load_whitelist()
        logger.info(f"[BiliVerifyFeishu] 插件已初始化，白名单群数: {len(whitelist)}")

        enable_startup_scan = self._safe_bool(
            self._get_config("ENABLE_STARTUP_REQUEST_SCAN", True),
            default=True,
        )
        if enable_startup_scan:
            startup_scan_limit = self._safe_int(
                self._get_config("STARTUP_REQUEST_SCAN_LIMIT", 50),
                default=50,
                minimum=1,
            )
            asyncio.create_task(
                self._deferred_startup_scan(startup_scan_limit)
            )

        enable_pending_check = self._safe_bool(
            self._get_config("ENABLE_PENDING_CHECK", True),
            default=True,
        )
        if enable_pending_check:
            check_interval = self._safe_int(
                self._get_config("PENDING_CHECK_INTERVAL", 60),
                default=60,
                minimum=10,
            )
            self._pending_check_task = asyncio.create_task(
                self._periodic_pending_check(check_interval)
            )
            logger.info(
                "[BiliVerifyFeishu] 已启动未处理入群请求巡检任务，"
                f"间隔: {check_interval}s"
            )
        else:
            logger.info("[BiliVerifyFeishu] 未处理入群请求巡检已关闭")

        # 启动 QQ 掉线检测（飞书通知）
        enable_offline = self._safe_bool(
            self._get_config("ENABLE_OFFLINE_NOTIFY", False),
            default=False,
        )
        if enable_offline:
            raw_targets = self._get_config("OFFLINE_FEISHU_TARGETS", [])
            has_targets = False
            if isinstance(raw_targets, list):
                has_targets = any(str(t).strip() for t in raw_targets)
            elif isinstance(raw_targets, str):
                has_targets = bool(raw_targets.strip())
            if not has_targets:
                logger.warning("[BiliVerifyFeishu] 已启用掉线检测但未配置 OFFLINE_FEISHU_TARGETS，掉线时仅记录日志")
            offline_interval = self._safe_int(
                self._get_config("OFFLINE_CHECK_INTERVAL", 60),
                default=60,
                minimum=10,
            )
            self._offline_monitor_task = asyncio.create_task(
                self._offline_monitor_loop(offline_interval)
            )
            logger.info(f"[BiliVerifyFeishu] 已启动QQ掉线检测任务，间隔: {offline_interval}s（飞书通知）")
        else:
            logger.info("[BiliVerifyFeishu] QQ掉线检测（飞书通知）已关闭")

    async def _periodic_pending_check(self, interval_seconds: int):
        """定时巡检白名单群中的未处理入群请求。"""
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                await self._check_unprocessed_requests()
        except asyncio.CancelledError:
            logger.info("[BiliVerifyFeishu] 未处理入群请求巡检任务已停止")
            raise
        except Exception as e:
            logger.error(f"[BiliVerifyFeishu] 巡检任务异常退出: {e}")

    async def _check_unprocessed_requests(self):
        """检查白名单群未处理数据并输出告警日志，兼做掉线期间漏扫补偿。"""
        whitelist = set(load_whitelist())
        if not whitelist:
            return

        pending_uid_entries = [
            key
            for key in self._pending_uid
            if key.split(":", 1)[0] in whitelist
        ]
        pending_records = [
            record
            for record in load_pending()
            if str(record.get("group_id", "")) in whitelist
        ]

        if pending_uid_entries or pending_records:
            sample_uid = ", ".join(pending_uid_entries[:3])
            pending_groups = sorted(
                {
                    str(record.get("group_id", ""))
                    for record in pending_records
                    if str(record.get("group_id", ""))
                }
            )
            sample_groups = ", ".join(pending_groups[:3])

            logger.warning(
                "[BiliVerifyFeishu] 发现白名单群未处理入群请求: "
                f"待补UID={len(pending_uid_entries)}"
                f"{f'({sample_uid})' if sample_uid else ''}, "
                f"写入失败待处理={len(pending_records)}"
                f"{f'({sample_groups})' if sample_groups else ''}"
            )

        # 补偿扫描：尝试拉取 get_group_system_msg 处理掉线期间积压的请求
        # 避免与启动扫描和掉线恢复扫描重复，用 _processed_request_keys 去重
        try:
            limit = self._safe_int(self._get_config("STARTUP_REQUEST_SCAN_LIMIT", 50), default=50, minimum=1)
            await self._scan_unhandled_group_requests_on_startup(limit)
        except Exception as e:
            logger.debug(f"[BiliVerifyFeishu] 周期补偿扫描失败: {e}")

    def _get_aiocqhttp_client(self, event: AstrMessageEvent | None = None):
        """获取 aiocqhttp 客户端实例。"""
        client = getattr(event, "bot", None) if event is not None else None
        if client is not None:
            return client

        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if platform is not None and hasattr(platform, "get_client"):
                return platform.get_client()
        except Exception:
            return None
        return None

    def _extract_group_requests_from_system_msg(self, payload: Any) -> list[dict]:
        """从 get_group_system_msg 返回值中提取请求列表。"""
        if isinstance(payload, dict):
            data = payload.get("data", payload)
        else:
            data = payload

        if not isinstance(data, dict):
            return []

        requests: list[dict] = []

        for req in data.get("join_requests", []) or []:
            if isinstance(req, dict):
                item = dict(req)
                item.setdefault("sub_type", "add")
                requests.append(item)

        for req in data.get("invited_requests", []) or []:
            if isinstance(req, dict):
                item = dict(req)
                item.setdefault("sub_type", "invite")
                requests.append(item)

        # 兼容某些实现返回统一 requests 数组
        for req in data.get("requests", []) or []:
            if isinstance(req, dict):
                requests.append(dict(req))

        return requests

    async def _resolve_nickname(
        self,
        user_id: str,
        event: AstrMessageEvent | None = None,
        raw: dict | None = None,
    ) -> str:
        """解析用户昵称：优先事件/原始字段，兜底查询 OneBot 陌生人信息。"""
        if raw is None:
            raw = {}

        # 1) 常见上报字段优先
        sender = raw.get("sender", {}) if isinstance(raw.get("sender", {}), dict) else {}
        for value in (
            sender.get("card"),
            sender.get("nickname"),
            raw.get("nickname"),
            raw.get("requester_nick"),
            raw.get("requester_nickname"),
            raw.get("user_name"),
            raw.get("nick"),
        ):
            text = str(value or "").strip()
            if text:
                return text

        # 2) 兜底调用 OneBot 获取陌生人昵称
        client = self._get_aiocqhttp_client(event)
        if client is None or not hasattr(client, "api"):
            return ""

        try:
            ret = await client.api.call_action("get_stranger_info", user_id=int(user_id))
        except Exception:
            return ""

        payload = ret.get("data", ret) if isinstance(ret, dict) else {}
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("nickname", "")).strip()

    async def _deferred_startup_scan(self, limit: int):
        """延迟执行启动补偿扫描，等待平台适配器就绪。"""
        for _ in range(6):
            await asyncio.sleep(1)
            client = self._get_aiocqhttp_client()
            if client is not None and hasattr(client, "api"):
                break
        await self._scan_unhandled_group_requests_on_startup(limit)

    async def _scan_unhandled_group_requests_on_startup(self, limit: int):
        """启动时补偿扫描未处理加群请求（覆盖插件加载前的请求）。"""
        client = self._get_aiocqhttp_client()
        if client is None or not hasattr(client, "api"):
            logger.warning("[BiliVerifyFeishu] 启动补偿扫描失败: 无法获取 aiocqhttp 客户端")
            return

        try:
            ret = await client.api.call_action("get_group_system_msg")
        except Exception as e:
            logger.warning(f"[BiliVerifyFeishu] 启动补偿扫描失败: {e}")
            return

        requests = self._extract_group_requests_from_system_msg(ret)
        if not requests:
            logger.info("[BiliVerifyFeishu] 启动补偿扫描完成: 未发现待处理加群请求")
            return

        handled = 0
        for req in requests:
            if handled >= limit:
                break

            if req.get("checked") is True:
                continue

            group_id = str(req.get("group_id", "")).strip()
            user_id = str(
                req.get("user_id")
                or req.get("requester_uin")
                or req.get("invitor_uin")
                or ""
            ).strip()
            flag = str(req.get("flag") or req.get("request_id") or "").strip()
            sub_type = str(req.get("sub_type") or req.get("type") or "add").strip()
            comment = str(req.get("comment") or req.get("message") or "").strip()

            if not group_id or not user_id or not flag:
                continue

            req_key = f"{group_id}:{user_id}:{flag}:{sub_type}"
            if req_key in self._processed_request_keys:
                continue

            raw = {
                "post_type": "request",
                "request_type": "group",
                "group_id": group_id,
                "user_id": user_id,
                "flag": flag,
                "sub_type": sub_type,
                "comment": comment,
            }
            await self._on_group_request(None, raw)
            handled += 1

        logger.info(
            "[BiliVerifyFeishu] 启动补偿扫描完成: "
            f"发现={len(requests)}, 已处理={handled}, 限额={limit}"
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_group_event(self, event: AstrMessageEvent):
        """处理 OneBot 消息、通知与请求事件。"""
        if event.get_platform_name() != "aiocqhttp":
            return

        raw = event.message_obj.raw_message
        if raw is None:
            return
        post_type = raw.get("post_type")

        if post_type == "request":
            request_type = raw.get("request_type")
            if request_type == "group":
                await self._on_group_request(event, raw)
        elif post_type == "notice":
            notice_type = raw.get("notice_type")
            if notice_type == "group_increase":
                await self._on_member_increase(event, raw)
            elif notice_type == "group_decrease":
                await self._on_member_decrease(event, raw)
        elif post_type == "message" and raw.get("message_type") == "group":
            await self._on_group_message(event, raw)

    def _get_status_config(self) -> tuple[str, str, str]:
        """读取状态字段配置。"""
        status_field = str(self._get_config("FEISHU_STATUS_FIELD", "状态") or "状态").strip()
        active_value = str(self._get_config("FEISHU_STATUS_ACTIVE_VALUE", "在群") or "在群").strip()
        left_value = str(self._get_config("FEISHU_STATUS_LEFT_VALUE", "已退群") or "已退群").strip()
        return status_field, active_value, left_value

    def _build_fields_for_join(
        self,
        uid_num: int,
        qq_num: int,
        nickname: str,
        time_ms: int,
    ) -> dict[str, Any]:
        """构造入群登记写入字段，包含成员状态。"""
        status_field, active_value, _ = self._get_status_config()
        return {
            "UID": uid_num,
            "QQ号": qq_num,
            "昵称": nickname,
            "时间": time_ms,
            status_field: active_value,
        }

    async def _set_group_add_request(
        self,
        event: AstrMessageEvent | None,
        flag: str,
        sub_type: str,
        approve: bool,
        reason: str = "",
    ) -> bool:
        """调用 OneBot API 处理加群请求/邀请。"""
        payload: dict[str, Any] = {
            "flag": flag,
            "sub_type": sub_type,
            "approve": approve,
        }
        if not approve and reason:
            payload["reason"] = reason

        client = self._get_aiocqhttp_client(event)

        if client is None or not hasattr(client, "api"):
            logger.error("[BiliVerifyFeishu] 处理加群请求失败: 无法获取 aiocqhttp 客户端")
            return False

        try:
            ret = await client.api.call_action("set_group_add_request", **payload)
            logger.info(
                "[BiliVerifyFeishu] 已处理加群请求: "
                f"approve={approve}, sub_type={sub_type}, ret={ret}"
            )
            return True
        except Exception as e:
            logger.error(f"[BiliVerifyFeishu] 处理加群请求失败: {e}")
            return False

    async def _on_group_request(self, event: AstrMessageEvent | None, raw: dict):
        """处理加群请求事件（request_type=group）。"""
        # 防 gank：随机延迟后再处理，避免被批量请求打爆飞书限流
        delay_min = self._safe_float(self._get_config("REQUEST_DELAY_MIN_SECONDS", 10.0), default=10.0, minimum=0.0)
        delay_max = self._safe_float(self._get_config("REQUEST_DELAY_MAX_SECONDS", 60.0), default=60.0, minimum=0.0)
        if delay_max < delay_min:
            delay_max = delay_min
        if delay_min > 0 or delay_max > 0:
            delay = random.uniform(delay_min, delay_max) if delay_max > delay_min else delay_min
            if delay > 0:
                logger.debug(f"[BiliVerifyFeishu] 入群请求随机延迟 {delay:.2f}s: group={raw.get('group_id')}, user={raw.get('user_id')}")
                await asyncio.sleep(delay)

        group_id = str(raw.get("group_id", ""))
        user_id = str(raw.get("user_id", ""))
        flag = str(raw.get("flag", "")).strip()
        sub_type = str(raw.get("sub_type", "add") or "add").strip()

        if not group_id or not user_id or not flag:
            return

        req_key = f"{group_id}:{user_id}:{flag}:{sub_type}"
        if req_key in self._processed_request_keys:
            return
        self._processed_request_keys.add(req_key)

        if not is_group_whitelisted(group_id):
            logger.info(
                "[BiliVerifyFeishu] 非白名单群加群请求，忽略: "
                f"group={group_id}, user={user_id}"
            )
            return

        comment = str(raw.get("comment", "")).strip()
        uid = self._extract_uid(comment)

        if uid is None:
            # 申请备注中未提供 UID，保留为待补 UID 用户。
            self._pending_uid.add(f"{group_id}:{user_id}")
            logger.info(
                "[BiliVerifyFeishu] 捕获白名单群加群请求，但备注无有效UID: "
                f"group={group_id}, user={user_id}"
            )
            await self._set_group_add_request(
                event,
                flag=flag,
                sub_type=sub_type,
                approve=False,
                reason="请在入群验证信息中提供B站UID",
            )
            return

        uid_num = int(uid)
        qq_num = int(user_id)
        time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        nickname = await self._resolve_nickname(user_id=user_id, event=event, raw=raw)

        fields = self._build_fields_for_join(
            uid_num=uid_num,
            qq_num=qq_num,
            nickname=nickname,
            time_ms=time_ms,
        )

        success = await upsert_member_row_by_qq_with_retry(
            fields=fields,
            qq_num=qq_num,
            config=self.config,
            qq_field_name="QQ号",
        )
        if success:
            logger.info(
                "[BiliVerifyFeishu] 加群请求备注UID写入成功: "
                f"UID={uid}, QQ={user_id}, 群={group_id}"
            )
            # 该用户已在请求阶段完成 UID 校验，入群通知到达时不应再加入待补集合。
            self._verified_before_join.add(f"{group_id}:{user_id}")
            self._pending_uid.discard(f"{group_id}:{user_id}")
            await self._set_group_add_request(
                event,
                flag=flag,
                sub_type=sub_type,
                approve=True,
            )
        else:
            logger.error(
                "[BiliVerifyFeishu] 加群请求备注UID写入失败，已加入待处理队列: "
                f"UID={uid}, QQ={user_id}, 群={group_id}"
            )
            add_to_pending(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "group_id": group_id,
                    "user_id": user_id,
                    "uid": uid,
                    "nickname": nickname,
                    "retry_count": 0,
                }
            )
            # 飞书不可达时不拒绝用户，先放行并入待处理队列，后续由巡检/重连扫描补偿
            logger.warning(
                f"[BiliVerifyFeishu] 飞书写入失败但仍放行用户，待后续补偿: UID={uid}, QQ={user_id}, 群={group_id}"
            )
            await self._set_group_add_request(
                event,
                flag=flag,
                sub_type=sub_type,
                approve=True,
            )

    async def _on_member_increase(self, event: AstrMessageEvent, raw: dict):
        """处理新成员入群事件。"""
        group_id = str(raw.get("group_id", ""))
        user_id = str(raw.get("user_id", ""))

        if not is_group_whitelisted(group_id):
            return

        key = f"{group_id}:{user_id}"
        if key in self._verified_before_join:
            self._verified_before_join.discard(key)
            self._pending_uid.discard(key)
            logger.info(
                "[BiliVerifyFeishu] 用户已在入群请求阶段完成UID校验，"
                f"跳过待补记录: group={group_id}, user={user_id}"
            )
            return

        logger.info(f"[BiliVerifyFeishu] 用户 {user_id} 加入白名单群 {group_id}")
        self._pending_uid.add(key)

    async def _on_member_decrease(self, event: AstrMessageEvent, raw: dict):
        """处理成员退群事件并回写飞书状态。"""
        group_id = str(raw.get("group_id", ""))
        user_id = str(raw.get("user_id", ""))
        sub_type = str(raw.get("sub_type", ""))

        if not group_id or not user_id or not is_group_whitelisted(group_id):
            return

        key = f"{group_id}:{user_id}"
        self._pending_uid.discard(key)
        self._verified_before_join.discard(key)

        try:
            qq_num = int(user_id)
        except ValueError:
            logger.warning(
                f"[BiliVerifyFeishu] 退群事件 user_id 非数字，跳过状态回写: {user_id}"
            )
            return

        status_field, _, left_value = self._get_status_config()
        success = await update_member_status_by_qq_with_retry(
            qq_num=qq_num,
            status_value=left_value,
            config=self.config,
            qq_field_name="QQ号",
            status_field_name=status_field,
        )

        if success:
            logger.info(
                "[BiliVerifyFeishu] 成员退群状态回写成功: "
                f"group={group_id}, user={user_id}, sub_type={sub_type}, status={left_value}"
            )
        else:
            logger.error(
                "[BiliVerifyFeishu] 成员退群状态回写失败: "
                f"group={group_id}, user={user_id}, sub_type={sub_type}, status={left_value}"
            )

    async def _on_group_message(self, event: AstrMessageEvent, raw: dict):
        """处理群聊消息，提取 B站 UID 并写入飞书。"""
        group_id = str(raw.get("group_id", ""))
        user_id = str(event.get_sender_id())

        if not is_group_whitelisted(group_id):
            return

        key = f"{group_id}:{user_id}"
        if key not in self._pending_uid:
            return

        text = event.message_str.strip()
        uid = self._extract_uid(text)

        if uid is None:
            return

        # 从待处理集合中移除
        self._pending_uid.discard(key)

        nickname = await self._resolve_nickname(user_id=user_id, event=event, raw=raw)

        logger.info(
            f"[BiliVerifyFeishu] 提取到 UID: {uid}, 用户: {user_id}({nickname}), 群: {group_id}"
        )

        uid_num = int(uid)
        qq_num = int(user_id)
        time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        # 构造写入飞书的字段数据
        fields = self._build_fields_for_join(
            uid_num=uid_num,
            qq_num=qq_num,
            nickname=nickname,
            time_ms=time_ms,
        )

        # 写入飞书（带重试）
        success = await upsert_member_row_by_qq_with_retry(
            fields=fields,
            qq_num=qq_num,
            config=self.config,
            qq_field_name="QQ号",
        )

        if success:
            logger.info(f"[BiliVerifyFeishu] 飞书写入成功: UID={uid}, QQ={user_id}")
        else:
            logger.error(
                f"[BiliVerifyFeishu] 飞书写入失败，已加入待处理队列: UID={uid}, QQ={user_id}"
            )
            # 写入失败时加入待处理队列
            add_to_pending(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "group_id": group_id,
                    "user_id": user_id,
                    "uid": uid,
                    "nickname": nickname,
                    "retry_count": 0,
                }
            )

    def _extract_uid(self, text: str) -> str | None:
        """从文本中提取 UID 数字部分（至少 6 位）。

        规则：
        1) 优先提取任意位置连续 6 位及以上数字。
        2) 若不存在连续片段，则拼接文本中所有数字后再判断。
        3) 少于 6 位视为无效 UID。
        """
        stripped = text.strip()

        # 允许中英文冒号、空格或任意前后缀，只要包含连续 6 位以上数字即可。
        contiguous = re.search(r"(\d{6,})", stripped)
        if contiguous:
            return contiguous.group(1)

        # 兼容被空格/符号拆开的数字，例如 "uid: 12 34 56"。
        merged_digits = "".join(re.findall(r"\d+", stripped))
        if len(merged_digits) >= 6:
            return merged_digits

        return None

    # ---- QQ 掉线检测（OneBot get_status + 飞书通知） ----

    async def _is_qq_online(self) -> bool:
        """通过 OneBot API 探测 QQ 是否在线。返回 True 表示在线。

        依据 OneBot 11 协议文档:
        - get_status 返回 {online: bool, good: bool}
        - get_login_info 在离线时通常抛异常或返回空
        同时兼容通过 aiocqhttp 底层 ws 客户端是否为空的前置判断。
        """
        client = self._get_aiocqhttp_client()
        if client is None or not hasattr(client, "api"):
            return False

        try:
            api_clients = getattr(client, "_wsr_api_clients", None)
            event_clients = getattr(client, "_wsr_event_clients", None)
            if isinstance(api_clients, dict) and isinstance(event_clients, set):
                if not api_clients and not event_clients:
                    return False
        except Exception:
            pass

        try:
            ret = await client.api.call_action("get_status")
            payload = ret.get("data", ret) if isinstance(ret, dict) else ret
            if isinstance(payload, dict):
                if "online" in payload:
                    return bool(payload.get("online")) and bool(payload.get("good", True))
                if "good" in payload:
                    return bool(payload.get("good"))
                if "stat" in payload:
                    return True
                return True
            return True
        except Exception as e:
            logger.debug(f"[BiliVerifyFeishu] get_status 探测失败: {e}")
            return False

    async def _offline_monitor_loop(self, interval_seconds: int):
        """定时探测 QQ 在线状态，阈值触发后通过飞书通知，恢复后补偿扫描。"""
        threshold = self._safe_int(self._get_config("OFFLINE_THRESHOLD", 3), default=3, minimum=1)
        await asyncio.sleep(10)
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    online = await self._is_qq_online()
                except Exception as e:
                    logger.warning(f"[BiliVerifyFeishu] 掉线检测异常: {e}")
                    online = False

                if online:
                    if self._offline_is_down:
                        self._offline_is_down = False
                        self._offline_fail_count = 0
                        logger.info("[BiliVerifyFeishu] QQ已恢复在线，执行补偿扫描")
                        if self._safe_bool(self._get_config("OFFLINE_RECOVERY_NOTIFY", True), default=True):
                            await self._notify_recovery_via_feishu()
                        # 补偿扫描掉线期间积压的加群请求
                        try:
                            limit = self._safe_int(self._get_config("STARTUP_REQUEST_SCAN_LIMIT", 50), default=50, minimum=1)
                            await self._scan_unhandled_group_requests_on_startup(limit)
                        except Exception as e:
                            logger.warning(f"[BiliVerifyFeishu] 恢复后补偿扫描失败: {e}")
                    else:
                        self._offline_fail_count = 0
                else:
                    self._offline_fail_count += 1
                    logger.warning(
                        f"[BiliVerifyFeishu] QQ离线探测失败 {self._offline_fail_count}/{threshold}"
                    )
                    if self._offline_fail_count >= threshold and not self._offline_is_down:
                        self._offline_is_down = True
                        logger.error("[BiliVerifyFeishu] 判定QQ机器人掉线，将通过飞书通知")
                        await self._notify_offline_via_feishu()
        except asyncio.CancelledError:
            logger.info("[BiliVerifyFeishu] QQ掉线检测任务已停止")
            raise
        except Exception as e:
            logger.error(f"[BiliVerifyFeishu] 掉线检测任务异常退出: {e}")

    async def _notify_offline_via_feishu(self):
        """通过飞书发送掉线告警。"""
        ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        whitelist = load_whitelist()
        pending_cnt = len([k for k in self._pending_uid if k.split(":", 1)[0] in set(whitelist)])
        content = (
            f"⚠️ QQ机器人掉线告警\n"
            f"时间: {ts}\n"
            f"连续失败: {self._offline_fail_count} 次\n"
            f"白名单群: {len(whitelist)} 个\n"
            f"待补UID: {pending_cnt} 条\n"
            f"请检查 NapCat/OneBot 连接与网络。"
        )
        try:
            sent = await broadcast_feishu_message(content=content, config=self.config)
            if sent == 0:
                logger.warning("[BiliVerifyFeishu] 飞书掉线告警未发送（无目标或失败），已记录日志")
        except Exception as e:
            logger.error(f"[BiliVerifyFeishu] 飞书掉线告警发送异常: {e}")

    async def _notify_recovery_via_feishu(self):
        """通过飞书发送恢复通知。"""
        ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        content = f"✅ QQ机器人已恢复在线\n时间: {ts}\n"
        try:
            await broadcast_feishu_message(content=content, config=self.config)
        except Exception as e:
            logger.error(f"[BiliVerifyFeishu] 飞书恢复通知发送异常: {e}")

    async def terminate(self):
        """插件销毁。"""
        if self._pending_check_task is not None:
            self._pending_check_task.cancel()
            try:
                await self._pending_check_task
            except asyncio.CancelledError:
                pass
            self._pending_check_task = None
        if self._offline_monitor_task is not None:
            self._offline_monitor_task.cancel()
            try:
                await self._offline_monitor_task
            except asyncio.CancelledError:
                pass
            self._offline_monitor_task = None
        logger.info("[BiliVerifyFeishu] 插件已卸载")
