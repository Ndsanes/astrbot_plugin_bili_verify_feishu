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

# 深 modules — narrow interface behind seam（admission 整合后逐步收口）
try:
    from .admission import AdmissionService
    from .admissions_store import AdmissionsStore
    from .member_registry import MemberRegistry
    from .platform_port import JoinRequest, OneBotAdapter, PlatformPort, QqOfficialAdapter
    from .plugin_config import PluginConfig
except Exception:  # pragma: no cover - 离线测试回退
    AdmissionService = None  # type: ignore
    AdmissionsStore = None  # type: ignore
    MemberRegistry = None  # type: ignore
    JoinRequest = None  # type: ignore
    PlatformPort = None  # type: ignore
    PluginConfig = None  # type: ignore


@register(
    "bili_verify_feishu",
    "NDsans",
    "QQ入群请求自动登记B站UID到飞书多维表格",
    "0.0.4",
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
        # QQ 官方入群申请轮询
        self._qqofficial_poll_task: asyncio.Task | None = None
        # QQ 掉线检测（通过飞书通知）
        self._offline_monitor_task: asyncio.Task | None = None
        self._offline_fail_count: int = 0
        self._offline_is_down: bool = False
        # 深 modules（initialize 时按 PluginConfig 实例化，提供 seam）
        self._typed_config: Any | None = None
        self._admissions_store: Any | None = None
        self._member_registry: Any | None = None
        self._admission_service: Any | None = None
        self._platform_port: Any | None = None  # PlatformPort interface，两个 adapter 复用同一 seam
        # qq_official 轮询：无效/不可拉取群缓存（数字群号、UMO 全串、已注销群），避免每轮重复报错
        self._qqofficial_invalid_groups: set[str] = set()

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

        # 初始化深 modules（失败则回落到原浅路径，保持兼容）
        try:
            if PluginConfig is not None:
                self._typed_config = PluginConfig.from_dict(dict(self.config) if isinstance(self.config, dict) else {})
            if AdmissionsStore is not None:
                self._admissions_store = AdmissionsStore()
            if MemberRegistry is not None:
                # 优先用 typed config 的 feishu 段
                cfg_for_registry = dict(self.config) if isinstance(self.config, dict) else {}
                # 适配 MemberRegistry.from_plugin_config 也可
                self._member_registry = MemberRegistry(cfg_for_registry)
            if AdmissionService is not None and self._member_registry is not None and self._admissions_store is not None and self._typed_config is not None:
                self._admission_service = AdmissionService(
                    registry=self._member_registry,
                    store=self._admissions_store,
                    config=self._typed_config,
                )
            # PlatformPort 按需实例化（两个 adapter 同一 seam）
            if QqOfficialAdapter is not None and OneBotAdapter is not None:
                # 仅作占位，实际 list/approve 时按平台选择 adapter
                self._platform_port = {
                    "qq_official": QqOfficialAdapter,
                    "onebot": OneBotAdapter,
                }
            logger.info("[BiliVerifyFeishu] 深 modules 已就绪：MemberRegistry / AdmissionsStore / AdmissionService / PlatformPort")
        except Exception as e:
            logger.warning(f"[BiliVerifyFeishu] 深 modules 初始化失败，回落浅路径: {e}")

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

        # 启动 QQ 官方入群申请轮询（需主动拉取，官方文档要求轮询）
        enable_qq_poll = self._safe_bool(
            self._get_config("ENABLE_QQOFFICIAL_JOIN_POLL", True),
            default=True,
        )
        if enable_qq_poll:
            poll_interval = self._safe_int(
                self._get_config("QQOFFICIAL_POLL_INTERVAL", 30),
                default=30,
                minimum=10,
            )
            self._qqofficial_poll_task = asyncio.create_task(
                self._qqofficial_join_poll_loop(poll_interval)
            )
            logger.info(f"[BiliVerifyFeishu] 已启动QQ官方入群申请轮询，间隔: {poll_interval}s（仅 qq_official 生效，aiocqhttp 下空转）")
        else:
            logger.info("[BiliVerifyFeishu] QQ官方入群申请轮询已关闭")

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
        """获取 aiocqhttp 客户端实例。兼容旧调用入口。"""
        return self._get_platform_client(filter.PlatformAdapterType.AIOCQHTTP, event)

    def _get_platform_client(self, platform_type, event: AstrMessageEvent | None = None):
        """按平台类型获取客户端，兼容 aiocqhttp / qq_official。"""
        # 1) 事件上直接携带的 bot（AstrMessageEvent.bot）
        client = getattr(event, "bot", None) if event is not None else None
        if client is not None:
            return client
        try:
            platform = self.context.get_platform(platform_type)
            if platform is not None and hasattr(platform, "get_client"):
                return platform.get_client()
        except Exception:
            return None
        return None

    def _get_qqofficial_client(self, event: AstrMessageEvent | None = None):
        """获取 qq_official 客户端（botpy.Client）。"""
        return self._get_platform_client(filter.PlatformAdapterType.QQOFFICIAL, event)

    def _has_platform(self, platform_type) -> bool:
        """检查指定平台是否已在 AstrBot 中注册。"""
        try:
            platform = self.context.get_platform(platform_type)
            return platform is not None
        except Exception:
            return False

    def _has_aiocqhttp(self) -> bool:
        return self._has_platform(filter.PlatformAdapterType.AIOCQHTTP)

    def _has_qqofficial(self) -> bool:
        return self._has_platform(filter.PlatformAdapterType.QQOFFICIAL)

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
        """解析用户昵称：优先事件/原始字段，兜底查询。兼容 aiocqhttp 与 qq_official。"""
        # qq_official 路径：优先从 AstrMessageEvent 拿
        if event is not None:
            try:
                name = event.get_sender_name()
                if name and str(name).strip():
                    return str(name).strip()
                # AstrBotMessage.sender.nickname 可能在 message_obj 中
                sender = getattr(event.message_obj, "sender", None)
                if sender is not None:
                    nick = getattr(sender, "nickname", "") or getattr(sender, "name", "")
                    if nick and str(nick).strip():
                        return str(nick).strip()
            except Exception:
                pass

        if raw is None:
            raw = {}

        if isinstance(raw, dict):
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
            # 兼容部分 qq_official 原始对象可能携带 author 字段
            for key in ("author", "member"):
                obj = raw.get(key)
                if isinstance(obj, dict):
                    for v in (obj.get("username"), obj.get("nick"), obj.get("name")):
                        if v and str(v).strip():
                            return str(v).strip()

        # OneBot 兜底：get_stranger_info（仅 aiocqhttp）
        client = self._get_aiocqhttp_client(event)
        if client is not None and hasattr(client, "api"):
            try:
                # openid 非数字时跳过
                if str(user_id).isdigit():
                    ret = await client.api.call_action("get_stranger_info", user_id=int(user_id))
                    payload = ret.get("data", ret) if isinstance(ret, dict) else {}
                    if isinstance(payload, dict):
                        return str(payload.get("nickname", "")).strip()
            except Exception:
                return ""
        return 

    async def _deferred_startup_scan(self, limit: int):
        """延迟执行启动补偿扫描，等待平台适配器就绪。仅 aiocqhttp 支持。"""
        if not self._has_aiocqhttp():
            logger.info("[BiliVerifyFeishu] 未检测到 aiocqhttp 平台，跳过启动补偿扫描（qq_official 不支持入群审批）")
            return
        for _ in range(6):
            await asyncio.sleep(1)
            client = self._get_aiocqhttp_client()
            if client is not None and hasattr(client, "api"):
                break
        await self._scan_unhandled_group_requests_on_startup(limit)

    async def _scan_unhandled_group_requests_on_startup(self, limit: int):
        """启动时补偿扫描未处理加群请求（覆盖插件加载前的请求）。仅 aiocqhttp 支持。"""
        if not self._has_aiocqhttp():
            logger.info("[BiliVerifyFeishu] 当前为 qq_official 模式，无需补偿扫描")
            return
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
        """处理群事件：兼容 aiocqhttp (OneBot) 与 qq_official (官方 WS)。"""
        platform_name = event.get_platform_name()
        raw = event.message_obj.raw_message

        # ---- QQ 官方机器人 (qq_official websocket) ----
        if platform_name == "qq_official":
            # 官方机器人仅支持群 @消息 / 私聊，无法拦截入群请求与成员增减
            # 入群后用户在群内发送 UID 即触发登记
            # AstrBot 已将 qq_official 的群消息映射为 GROUP_MESSAGE
            try:
                msg_type = event.message_obj.type
                from astrbot.api.platform import MessageType as _MT
                is_group = (msg_type == _MT.GROUP_MESSAGE)
            except Exception:
                is_group = True  # 兜底按群消息处理
            # 无 raw dict 时直接按群消息处理
            if raw is None:
                await self._on_group_message_qq_official(event)
                return
            # 若 raw 为 botpy 对象，转为群消息处理
            if not isinstance(raw, dict):
                await self._on_group_message_qq_official(event)
                return
            # 兼容极少数情况下 raw 仍为 dict 的场景
            await self._on_group_message_qq_official(event)
            return

        # ---- OneBot (aiocqhttp) ----
        if platform_name != "aiocqhttp":
            return

        if raw is None:
            return
        # 兼容 raw 可能是非 dict（如对象）的防御
        if not isinstance(raw, dict):
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
        qq_num: int | str,
        nickname: str,
        time_ms: int,
    ) -> dict[str, Any]:
        """构造入群登记写入字段，包含成员状态。兼容 qq_official 的 openid 字符串。"""
        status_field, active_value, _ = self._get_status_config()
        # QQ号字段：数字保持 int，openid 字符串保持 str
        qq_value: int | str = qq_num
        if isinstance(qq_num, str) and qq_num.isdigit():
            try:
                qq_value = int(qq_num)
            except ValueError:
                qq_value = qq_num
        return {
            "UID": uid_num,
            "QQ号": qq_value,
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
        # 兼容 openid 字符串
        qq_key: int | str = user_id
        if isinstance(user_id, str) and user_id.isdigit():
            try:
                qq_key = int(user_id)
            except ValueError:
                qq_key = user_id
        time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        nickname = await self._resolve_nickname(user_id=user_id, event=event, raw=raw)

        fields = self._build_fields_for_join(
            uid_num=uid_num,
            qq_num=qq_key,
            nickname=nickname,
            time_ms=time_ms,
        )

        success = await upsert_member_row_by_qq_with_retry(
            fields=fields,
            qq_num=qq_key,
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

        # 兼容 qq_official 的 openid 字符串：不再强制要求数字
        qq_key: int | str = user_id
        if isinstance(user_id, str) and user_id.isdigit():
            try:
                qq_key = int(user_id)
            except ValueError:
                qq_key = user_id

        status_field, _, left_value = self._get_status_config()
        success = await update_member_status_by_qq_with_retry(
            qq_num=qq_key,
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
        """处理群聊消息，提取 B站 UID 并写入飞书。（aiocqhttp 路径，需 pending 校验）"""
        # 兼容 raw 丢失或字段名差异：优先 event.get_group_id()
        group_id = ""
        if isinstance(raw, dict):
            group_id = str(raw.get("group_id", "")).strip()
        if not group_id:
            try:
                group_id = str(event.get_group_id() or "").strip()
            except Exception:
                group_id = ""
        user_id = str(event.get_sender_id())

        if not group_id or not is_group_whitelisted(group_id):
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
        qq_key2: int | str = user_id
        if isinstance(user_id, str) and user_id.isdigit():
            try:
                qq_key2 = int(user_id)
            except ValueError:
                qq_key2 = user_id
        time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        # 构造写入飞书的字段数据
        fields = self._build_fields_for_join(
            uid_num=uid_num,
            qq_num=qq_key2,
            nickname=nickname,
            time_ms=time_ms,
        )

        # 写入飞书（带重试）
        success = await upsert_member_row_by_qq_with_retry(
            fields=fields,
            qq_num=qq_key2,
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

    async def _on_group_message_qq_official(self, event: AstrMessageEvent):
        """处理 qq_official 群消息：无 pending 限制，直接提取 UID 并写入飞书。"""
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:
            group_id = ""
        if not group_id:
            try:
                group_id = str(getattr(event.message_obj, "group_id", "") or "").strip()
            except Exception:
                group_id = ""
        if not group_id or not is_group_whitelisted(group_id):
            return

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            return

        text = (event.message_str or "").strip()
        if not text:
            try:
                raw = event.message_obj.raw_message
                if raw is not None and hasattr(raw, "content"):
                    text = str(getattr(raw, "content", "") or "").strip()
            except Exception:
                pass
        if not text:
            return

        uid = self._extract_uid(text)
        if uid is None:
            return

        key = f"{group_id}:{user_id}"
        self._pending_uid.discard(key)

        nickname = await self._resolve_nickname(user_id=user_id, event=event, raw=None)
        logger.info(
            f"[BiliVerifyFeishu][qq_official] 提取到 UID: {uid}, 用户: {user_id}({nickname}), 群: {group_id}"
        )

        uid_num = int(uid)
        qq_key: int | str = user_id
        if user_id.isdigit():
            try:
                qq_key = int(user_id)
            except ValueError:
                qq_key = user_id
        time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        fields = self._build_fields_for_join(
            uid_num=uid_num,
            qq_num=qq_key,
            nickname=nickname,
            time_ms=time_ms,
        )

        success = await upsert_member_row_by_qq_with_retry(
            fields=fields,
            qq_num=qq_key,
            config=self.config,
            qq_field_name="QQ号",
        )

        if success:
            logger.info(f"[BiliVerifyFeishu][qq_official] 飞书写入成功: UID={uid}, QQ={user_id}")
        else:
            logger.error(
                f"[BiliVerifyFeishu][qq_official] 飞书写入失败，已加入待处理队列: UID={uid}, QQ={user_id}"
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
        """探测 QQ 是否在线。兼容 aiocqhttp (OneBot) 与 qq_official。

        - aiocqhttp: 通过 get_status / ws 客户端存在性判断
        - qq_official: 通过 get_platform 存活性 + botpy 客户端是否关闭判断（官方 WS 掉线时平台仍在但 client.is_closed）
        """
        # 优先尝试 aiocqhttp 路径
        if self._has_aiocqhttp():
            client = self._get_aiocqhttp_client()
            if client is None or not hasattr(client, "api"):
                # aiocqhttp 平台存在但客户端未就绪，视为离线
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

        # qq_official 路径
        if self._has_qqofficial():
            client = self._get_qqofficial_client()
            if client is None:
                return False
            # botpy Client 有 is_closed / _closed 等状态
            try:
                if hasattr(client, "is_closed"):
                    # is_closed 可能是方法或属性
                    is_closed = client.is_closed
                    if callable(is_closed):
                        is_closed = is_closed()
                    if is_closed:
                        return False
                # 额外检查平台底层心跳：尝试获取平台实例
                platform = self.context.get_platform(filter.PlatformAdapterType.QQOFFICIAL)
                if platform is not None:
                    # 若近期有事件，session 缓存非空也算在线依据（弱判断）
                    return True
                return True
            except Exception as e:
                logger.debug(f"[BiliVerifyFeishu] qq_official 在线探测失败: {e}")
                return False

        # 无可用平台则视为离线
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

    # ---- QQ 官方入群申请轮询（主动拉取） ----
    async def _qqofficial_join_poll_loop(self, interval_seconds: int):
        """定时轮询 qq_official 入群申请列表。"""
        # 等待适配器就绪
        for _ in range(6):
            await asyncio.sleep(1)
            if self._get_qqofficial_client() is not None:
                break
        await asyncio.sleep(5)
        logger.info("[BiliVerifyFeishu] QQ官方入群申请轮询已就绪，开始首次拉取")
        try:
            while True:
                try:
                    await self._poll_qqofficial_join_requests_once()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"[BiliVerifyFeishu] QQ官方入群轮询异常: {e}")
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("[BiliVerifyFeishu] QQ官方入群申请轮询已停止")
            raise
        except Exception as e:
            logger.error(f"[BiliVerifyFeishu] QQ官方入群轮询异常退出: {e}")

    @staticmethod
    def _is_valid_group_openid(g: str) -> bool:
        """判断是否为合法的 qq_official group_openid。

        官方 group_openid 为大写十六进制风格字符串（如 6CCC18AB28098F241B44FF1A41F6668F）。
        - 纯数字（aiocqhttp 群号）→ 不适用于官方接口
        - 含 ':' 或为 UMO 全串（default_xxx:GroupMessage:xxx）→ 不适用
        - 过短/含非法字符 → 视为无效
        """
        s = str(g or "").strip()
        if not s or ":" in s:
            return False
        if s.isdigit():
            return False
        # 官方 openid 常见长度 32；放宽为 16-64 的大写十六进制
        if not (8 <= len(s) <= 128):
            return False
        return True

    async def _poll_qqofficial_join_requests_once(self):
        """单次轮询所有白名单群的入群申请。自动过滤非 qq_official 的白名单项与已知无效群。"""
        whitelist = load_whitelist()
        if not whitelist:
            return
        # 频率控制：每个群间隔约 0.5s，避免触发 30QPM 限制
        for group_openid in whitelist:
            g = str(group_openid).strip()
            if not g:
                continue
            # 过滤：非官方 openid 格式 或 已知无效/不可达
            if g in self._qqofficial_invalid_groups:
                continue
            if not self._is_valid_group_openid(g):
                logger.debug(f"[BiliVerifyFeishu] 跳过非 qq_official 白名单项: {g}")
                self._qqofficial_invalid_groups.add(g)
                continue
            try:
                await self._poll_single_group_join_requests(g)
            except Exception as e:
                logger.debug(f"[BiliVerifyFeishu] 拉取群 {g} 入群申请失败: {e}")
            await asyncio.sleep(0.5)

    async def _poll_single_group_join_requests(self, group_openid: str):
        """拉取单个群的入群申请并处理。"""
        client = self._get_qqofficial_client()
        if client is None:
            logger.debug("[BiliVerifyFeishu] 无法获取 qq_official 客户端，跳过轮询")
            return
        # 兼容 botpy 版本差异：优先使用 BotAPI._http.request + Route
        try:
            from botpy.http import Route
        except Exception:
            logger.warning("[BiliVerifyFeishu] 未找到 botpy.http.Route，跳过QQ官方拉取")
            return

        http = getattr(getattr(client, "api", None), "_http", None)
        if http is None:
            # 兜底：client 本身可能就是 http
            http = getattr(client, "_http", None)
        if http is None:
            logger.warning("[BiliVerifyFeishu] 无法获取 qq_official http 客户端")
            return

        poll_limit = self._safe_int(self._get_config("QQOFFICIAL_POLL_LIMIT", 20), default=20, minimum=1)
        poll_limit = min(poll_limit, 100)
        cursor = ""
        # 随机延迟复用现有配置，防 gank
        delay_min = self._safe_float(self._get_config("REQUEST_DELAY_MIN_SECONDS", 0), default=0, minimum=0.0)
        delay_max = self._safe_float(self._get_config("REQUEST_DELAY_MAX_SECONDS", 0), default=0, minimum=0.0)
        # 限制单次轮询最大页数防止死循环
        max_pages = 5
        pages = 0
        while pages < max_pages:
            pages += 1
            route = Route("GET", "/v2/groups/{group_openid}/join_request_list", group_openid=group_openid)
            # 请求参数：cursor/limit 官方文档定义在请求体，但 botpy 的 GET 需用 params
            params: dict[str, str | int] = {}
            if cursor:
                params["cursor"] = cursor
            if poll_limit:
                params["limit"] = poll_limit
            try:
                # botpy 的 request 支持 params 关键字
                if params:
                    ret = await http.request(route, params=params)
                else:
                    ret = await http.request(route)
            except TypeError:
                # 兼容旧版 botpy 仅支持 json 参数的场景
                try:
                    ret = await http.request(route, json=params if params else None)
                except Exception as e:
                    msg = str(e)
                    logger.debug(f"[BiliVerifyFeishu] 请求入群列表失败 {group_openid} cursor={cursor}: {msg}")
                    if ("资源不存在" in msg) or ("replace query param" in msg) or ("注销" in msg):
                        self._qqofficial_invalid_groups.add(group_openid)
                        logger.info(f"[BiliVerifyFeishu] 群不可达，本轮起跳过轮询: {group_openid}")
                    return
            except Exception as e:
                msg = str(e)
                logger.debug(f"[BiliVerifyFeishu] 请求入群列表失败 {group_openid} cursor={cursor}: {msg}")
                if ("资源不存在" in msg) or ("replace query param" in msg) or ("注销" in msg):
                    self._qqofficial_invalid_groups.add(group_openid)
                    logger.info(f"[BiliVerifyFeishu] 群不可达，本轮起跳过轮询: {group_openid}")
                return

            if ret is None:
                return
            # 兼容部分实现返回 {data: {...}} 包裹
            payload = ret.get("data", ret) if isinstance(ret, dict) else ret
            if not isinstance(payload, dict):
                return
            req_list = payload.get("list", []) or []
            next_cursor = str(payload.get("next_cursor", "") or "").strip()

            if not req_list:
                # 无待处理
                if not next_cursor:
                    return
                cursor = next_cursor
                continue

            for item in req_list:
                if not isinstance(item, dict):
                    continue
                join_request_id = str(item.get("join_request_id", "")).strip()
                member_openid = str(item.get("member_openid", "")).strip()
                username = str(item.get("username", "")).strip()
                if not join_request_id or not member_openid:
                    continue
                # 去重：join_request_id 维度
                req_key = f"{group_openid}:{member_openid}:{join_request_id}"
                if req_key in self._processed_request_keys:
                    continue
                self._processed_request_keys.add(req_key)

                # 提取校验信息：verify_message 或 review_qa_list
                verify_info = item.get("verify_info", {}) if isinstance(item.get("verify_info", {}), dict) else {}
                comment = str(verify_info.get("verify_message", "") or "").strip()
                if not comment:
                    # 兼容问答模式：拼接所有答案
                    qa_list = verify_info.get("review_qa_list", []) or []
                    if isinstance(qa_list, list):
                        parts = []
                        for qa in qa_list:
                            if isinstance(qa, dict):
                                ans = str(qa.get("answer", "") or "").strip()
                                if ans:
                                    parts.append(ans)
                        comment = " ".join(parts)

                # 防 gank 随机延迟
                if delay_min > 0 or delay_max > 0:
                    dm = delay_min
                    dx = delay_max
                    if dx < dm:
                        dx = dm
                    delay = random.uniform(dm, dx) if dx > dm else dm
                    if delay > 0:
                        await asyncio.sleep(delay)

                await self._handle_qqofficial_join_request(
                    group_openid=group_openid,
                    member_openid=member_openid,
                    join_request_id=join_request_id,
                    username=username,
                    comment=comment,
                    raw_item=item,
                )

            if not next_cursor:
                return
            cursor = next_cursor

    async def _handle_qqofficial_join_request(
        self,
        group_openid: str,
        member_openid: str,
        join_request_id: str,
        username: str,
        comment: str,
        raw_item: dict,
    ):
        """处理单条 qq_official 入群申请：校验 UID -> 飞书 -> 放行/拒绝。优先走深 AdmissionService seam。"""
        # 深路径：通过 AdmissionService 统一决策与落库（窄 interface）
        if self._admission_service is not None and JoinRequest is not None:
            try:
                req = JoinRequest(
                    group_openid=group_openid,
                    member_openid=member_openid,
                    join_request_id=join_request_id,
                    username=username,
                    comment=comment,
                    raw=raw_item or {},
                )
                result = await self._admission_service.admit_request(req)
                # 将 deep module 的决策映射回平台审批（seam 仍在 main 侧，保持两 adapter 可替换）
                if result.decision == "approve":
                    await self._qqofficial_approve_join_request(
                        group_openid=group_openid,
                        member_openid=member_openid,
                        join_request_id=join_request_id,
                        approve=True,
                    )
                elif result.reason == "not_whitelisted":
                    return
                else:
                    # decline 场景（无 UID 等）
                    reason = result.reason or "请在入群验证信息中提供B站UID"
                    await self._qqofficial_approve_join_request(
                        group_openid=group_openid,
                        member_openid=member_openid,
                        join_request_id=join_request_id,
                        approve=False,
                        reject_reason=reason,
                    )
                return
            except Exception as e:
                logger.warning(f"[BiliVerifyFeishu] 深 Admission 路径失败，回落浅路径: {e}")

        if not is_group_whitelisted(group_openid):
            logger.info(f"[BiliVerifyFeishu] 非白名单群入群申请，忽略: group={group_openid}, user={member_openid}")
            return

        uid = self._extract_uid(comment)
        if uid is None:
            logger.info(
                f"[BiliVerifyFeishu] 捕获白名单群入群请求但无有效UID，拒绝: group={group_openid}, user={member_openid}({username}), comment={comment!r}"
            )
            await self._qqofficial_approve_join_request(
                group_openid=group_openid,
                member_openid=member_openid,
                join_request_id=join_request_id,
                approve=False,
                reject_reason="请在入群验证信息中提供B站UID",
            )
            return

        uid_num = int(uid)
        qq_key: int | str = member_openid
        time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        nickname = username or await self._resolve_nickname(user_id=member_openid, event=None, raw=raw_item)

        fields = self._build_fields_for_join(
            uid_num=uid_num,
            qq_num=qq_key,
            nickname=nickname,
            time_ms=time_ms,
        )

        success = await upsert_member_row_by_qq_with_retry(
            fields=fields,
            qq_num=qq_key,
            config=self.config,
            qq_field_name="QQ号",
        )
        if success:
            logger.info(f"[BiliVerifyFeishu] QQ官方入群 UID 写入成功，准备放行: UID={uid}, openid={member_openid}, 群={group_openid}")
            self._verified_before_join.add(f"{group_openid}:{member_openid}")
            self._pending_uid.discard(f"{group_openid}:{member_openid}")
            await self._qqofficial_approve_join_request(
                group_openid=group_openid,
                member_openid=member_openid,
                join_request_id=join_request_id,
                approve=True,
            )
        else:
            logger.error(f"[BiliVerifyFeishu] QQ官方入群 UID 写入失败，已加入待处理: UID={uid}, openid={member_openid}")
            add_to_pending(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "group_id": group_openid,
                    "user_id": member_openid,
                    "uid": uid,
                    "nickname": nickname,
                    "retry_count": 0,
                    "join_request_id": join_request_id,
                }
            )
            logger.warning(f"[BiliVerifyFeishu] 飞书写入失败但仍放行（qq_official）: UID={uid}, openid={member_openid}")
            await self._qqofficial_approve_join_request(
                group_openid=group_openid,
                member_openid=member_openid,
                join_request_id=join_request_id,
                approve=True,
            )

    async def _qqofficial_approve_join_request(
        self,
        group_openid: str,
        member_openid: str,
        join_request_id: str,
        approve: bool,
        reject_reason: str = "",
    ) -> bool:
        """调用官方审批接口。"""
        client = self._get_qqofficial_client()
        if client is None:
            logger.error("[BiliVerifyFeishu] 审批失败: 无法获取 qq_official 客户端")
            return False
        try:
            from botpy.http import Route
        except Exception:
            logger.error("[BiliVerifyFeishu] 审批失败: 未找到 botpy.http.Route")
            return False
        http = getattr(getattr(client, "api", None), "_http", None)
        if http is None:
            http = getattr(client, "_http", None)
        if http is None:
            logger.error("[BiliVerifyFeishu] 审批失败: 无法获取 http 客户端")
            return False

        route = Route(
            "POST",
            "/v2/groups/{group_openid}/approval_join_request/{member_openid}",
            group_openid=group_openid,
            member_openid=member_openid,
        )
        payload: dict[str, str | bool] = {
            "op": "approve" if approve else "decline",
            "join_request_id": join_request_id,
        }
        if not approve and reject_reason:
            payload["reject_reason"] = reject_reason

        try:
            ret = await http.request(route, json=payload)
            logger.info(
                f"[BiliVerifyFeishu] 已处理QQ官方入群申请: approve={approve}, group={group_openid}, member={member_openid}, ret={ret}"
            )
            return True
        except Exception as e:
            logger.error(f"[BiliVerifyFeishu] 处理QQ官方入群申请失败: {e}")
            return False

    async def terminate(self):
        """插件销毁。"""
        if self._pending_check_task is not None:
            self._pending_check_task.cancel()
            try:
                await self._pending_check_task
            except asyncio.CancelledError:
                pass
            self._pending_check_task = None
        if self._qqofficial_poll_task is not None:
            self._qqofficial_poll_task.cancel()
            try:
                await self._qqofficial_poll_task
            except asyncio.CancelledError:
                pass
            self._qqofficial_poll_task = None
        if self._offline_monitor_task is not None:
            self._offline_monitor_task.cancel()
            try:
                await self._offline_monitor_task
            except asyncio.CancelledError:
                pass
            self._offline_monitor_task = None
        logger.info("[BiliVerifyFeishu] 插件已卸载")
