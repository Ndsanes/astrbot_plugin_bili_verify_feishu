"""
plugin_config — 深 module / 窄 interface / 高 leverage

职责（depth）：
  一次性解析 _conf_schema.json / AstrBot 注入的原始 dict，
  集中所有 _safe_int / _safe_bool / _safe_float 与钳制逻辑，
  提供有类型的只读 frozen dataclass。

隐藏的内部（seam 之后）：
  - key 名拼写、默认值、minimum 钳制
  - str/bool/int/float 互转与容错
  - QQOFFICIAL_POLL_LIMIT 上限 100 等业务约束
  - WHITELIST_GROUPS 兼容 list 与逗号分隔 str

对外暴露（窄 interface）：
  PluginConfig.from_dict(raw) -> PluginConfig
  cfg.feishu.app_token / cfg.poll.interval / cfg.delays.min_seconds
  cfg.whitelist / cfg.whitelist.groups / cfg.offline.enabled
  cfg.pending.interval / cfg.retry.max_retries

设计取舍（leverage / locality）：
  - leverage：一次解析，N 处有类型访问，无需重复 _get_config
  - locality：错误的 key/类型在启动时暴露，不在轮询循环中才暴露
  - 术语严格使用 module/interface/seam/adapter/depth/leverage/locality
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


def _safe_int(v: Any, default: int, minimum: int = 1) -> int:
    try:
        n = int(v)
    except Exception:
        return default
    return max(n, minimum)


def _safe_bool(v: Any, default: bool = True) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
    return default


def _safe_float(v: Any, default: float, minimum: float = 0.0) -> float:
    try:
        n = float(v)
    except Exception:
        return default
    return max(n, minimum)


def _as_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        # 兼容逗号分隔字符串
        if "," in v:
            return [g.strip() for g in v.split(",") if g.strip()]
        s = v.strip()
        return [s] if s else []
    if v is None:
        return []
    s = str(v).strip()
    return [s] if s else []


@dataclass(frozen=True, slots=True)
class FeishuCfg:
    app_id: str = ""
    app_secret: str = ""
    app_token: str = ""
    table_id: str = ""
    status_field: str = "状态"
    status_active: str = "在群"
    status_left: str = "已退群"
    qq_field: str = "QQ号"
    max_retries: int = 3
    retry_delay: float = 1.0


@dataclass(frozen=True, slots=True)
class PollCfg:
    enabled: bool = True
    interval: int = 30
    limit: int = 20
    startup_scan_enabled: bool = True
    startup_scan_limit: int = 50
    pending_check_enabled: bool = True
    pending_check_interval: int = 3800


@dataclass(frozen=True, slots=True)
class DelayCfg:
    min_seconds: float = 10.0
    max_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class WhitelistCfg:
    groups: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class OfflineCfg:
    enabled: bool = False
    targets: list[str] = field(default_factory=list)
    id_type: str = "open_id"
    check_interval: int = 60
    threshold: int = 3
    recovery_notify: bool = True


# 新增：按任务 Contract 显式拆出的深模块，seam 后提供更细粒度的 locality
@dataclass(frozen=True, slots=True)
class PendingCfg:
    enabled: bool = True
    interval: int = 3800
    startup_enabled: bool = True
    startup_limit: int = 50


@dataclass(frozen=True, slots=True)
class RetryCfg:
    max_retries: int = 3
    delay: float = 1.0


@dataclass(frozen=True, slots=True)
class PluginConfig:
    feishu: FeishuCfg = field(default_factory=FeishuCfg)
    poll: PollCfg = field(default_factory=PollCfg)
    delays: DelayCfg = field(default_factory=DelayCfg)
    whitelist: WhitelistCfg = field(default_factory=WhitelistCfg)
    offline: OfflineCfg = field(default_factory=OfflineCfg)
    pending: PendingCfg = field(default_factory=PendingCfg)
    retry: RetryCfg = field(default_factory=RetryCfg)
    # 原始 dict 保留，给需要透传 mapping 的旧 seam 兼容
    _raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "PluginConfig":
        raw = dict(raw or {})
        feishu = FeishuCfg(
            app_id=str(raw.get("FEISHU_APP_ID", "") or "").strip(),
            app_secret=str(raw.get("FEISHU_APP_SECRET", "") or "").strip(),
            app_token=str(raw.get("FEISHU_APP_TOKEN", "") or "").strip(),
            table_id=str(raw.get("FEISHU_TABLE_ID", "") or "").strip(),
            status_field=str(raw.get("FEISHU_STATUS_FIELD", "状态") or "状态").strip(),
            status_active=str(raw.get("FEISHU_STATUS_ACTIVE_VALUE", "在群") or "在群").strip(),
            status_left=str(raw.get("FEISHU_STATUS_LEFT_VALUE", "已退群") or "已退群").strip(),
            qq_field=str(raw.get("FEISHU_QQ_FIELD", "QQ号") or "QQ号").strip(),
            max_retries=_safe_int(raw.get("MAX_RETRIES", 3), 3, 1),
            retry_delay=_safe_float(raw.get("RETRY_DELAY", 1.0), 1.0, 0.0),
        )
        poll_interval = max(_safe_int(raw.get("QQOFFICIAL_POLL_INTERVAL", 30), 30, 10), 10)
        poll_limit = min(max(_safe_int(raw.get("QQOFFICIAL_POLL_LIMIT", 20), 20, 1), 1), 100)
        pending_interval = _safe_int(raw.get("PENDING_CHECK_INTERVAL", 3800), 3800, 10)
        startup_limit = _safe_int(raw.get("STARTUP_REQUEST_SCAN_LIMIT", 50), 50, 1)
        poll = PollCfg(
            enabled=_safe_bool(raw.get("ENABLE_QQOFFICIAL_JOIN_POLL", True), True),
            interval=poll_interval,
            limit=poll_limit,
            startup_scan_enabled=_safe_bool(raw.get("ENABLE_STARTUP_REQUEST_SCAN", True), True),
            startup_scan_limit=startup_limit,
            pending_check_enabled=_safe_bool(raw.get("ENABLE_PENDING_CHECK", True), True),
            pending_check_interval=pending_interval,
        )
        delays = DelayCfg(
            min_seconds=_safe_float(raw.get("REQUEST_DELAY_MIN_SECONDS", 10.0), 10.0, 0.0),
            max_seconds=_safe_float(raw.get("REQUEST_DELAY_MAX_SECONDS", 60.0), 60.0, 0.0),
        )
        # 钳制 max >= min
        if delays.max_seconds < delays.min_seconds:
            delays = DelayCfg(min_seconds=delays.min_seconds, max_seconds=delays.min_seconds)
        whitelist = WhitelistCfg(groups=_as_list(raw.get("WHITELIST_GROUPS", [])))
        # 去重保序
        seen: set[str] = set()
        uniq: list[str] = []
        for g in whitelist.groups:
            if g not in seen:
                seen.add(g)
                uniq.append(g)
        whitelist = WhitelistCfg(groups=uniq)
        offline = OfflineCfg(
            enabled=_safe_bool(raw.get("ENABLE_OFFLINE_NOTIFY", False), False),
            targets=_as_list(raw.get("OFFLINE_FEISHU_TARGETS", [])),
            id_type=(str(raw.get("OFFLINE_FEISHU_ID_TYPE", "open_id") or "open_id").strip() or "open_id"),
            check_interval=_safe_int(raw.get("OFFLINE_CHECK_INTERVAL", 60), 60, 10),
            threshold=_safe_int(raw.get("OFFLINE_THRESHOLD", 3), 3, 1),
            recovery_notify=_safe_bool(raw.get("OFFLINE_RECOVERY_NOTIFY", True), True),
        )
        # 归一化 id_type
        if offline.id_type not in {"open_id", "user_id", "union_id", "email", "chat_id"}:
            offline = OfflineCfg(
                enabled=offline.enabled,
                targets=offline.targets,
                id_type="open_id",
                check_interval=offline.check_interval,
                threshold=offline.threshold,
                recovery_notify=offline.recovery_notify,
            )
        pending = PendingCfg(
            enabled=poll.pending_check_enabled,
            interval=poll.pending_check_interval,
            startup_enabled=poll.startup_scan_enabled,
            startup_limit=poll.startup_scan_limit,
        )
        retry = RetryCfg(max_retries=feishu.max_retries, delay=feishu.retry_delay)
        return cls(feishu=feishu, poll=poll, delays=delays, whitelist=whitelist, offline=offline, pending=pending, retry=retry, _raw=raw)

    def validate(self) -> list[str]:
        errs: list[str] = []
        for k in ("app_id", "app_secret", "app_token", "table_id"):
            if not getattr(self.feishu, k):
                errs.append(f"缺少必要配置: FEISHU_{k.upper()}")
        return errs

    # 兼容 Mapping.get 语义，窄 interface 外的过渡 seam
    def get(self, key: str, default: Any = None) -> Any:
        mapping: dict[str, Any] = {
            "FEISHU_APP_ID": self.feishu.app_id,
            "FEISHU_APP_SECRET": self.feishu.app_secret,
            "FEISHU_APP_TOKEN": self.feishu.app_token,
            "FEISHU_TABLE_ID": self.feishu.table_id,
            "FEISHU_STATUS_FIELD": self.feishu.status_field,
            "FEISHU_STATUS_ACTIVE_VALUE": self.feishu.status_active,
            "FEISHU_STATUS_LEFT_VALUE": self.feishu.status_left,
            "FEISHU_QQ_FIELD": self.feishu.qq_field,
            "WHITELIST_GROUPS": list(self.whitelist.groups),
            "MAX_RETRIES": self.retry.max_retries,
            "RETRY_DELAY": self.retry.delay,
            "ENABLE_PENDING_CHECK": self.pending.enabled,
            "PENDING_CHECK_INTERVAL": self.pending.interval,
            "ENABLE_STARTUP_REQUEST_SCAN": self.pending.startup_enabled,
            "STARTUP_REQUEST_SCAN_LIMIT": self.pending.startup_limit,
            "ENABLE_OFFLINE_NOTIFY": self.offline.enabled,
            "OFFLINE_FEISHU_TARGETS": list(self.offline.targets),
            "OFFLINE_FEISHU_ID_TYPE": self.offline.id_type,
            "OFFLINE_CHECK_INTERVAL": self.offline.check_interval,
            "OFFLINE_THRESHOLD": self.offline.threshold,
            "OFFLINE_RECOVERY_NOTIFY": self.offline.recovery_notify,
            "REQUEST_DELAY_MIN_SECONDS": self.delays.min_seconds,
            "REQUEST_DELAY_MAX_SECONDS": self.delays.max_seconds,
            "ENABLE_QQOFFICIAL_JOIN_POLL": self.poll.enabled,
            "QQOFFICIAL_POLL_INTERVAL": self.poll.interval,
            "QQOFFICIAL_POLL_LIMIT": self.poll.limit,
        }
        return mapping.get(key, default)

    def as_dict(self) -> dict[str, Any]:
        return {
            "FEISHU_APP_ID": self.feishu.app_id,
            "FEISHU_APP_SECRET": self.feishu.app_secret,
            "FEISHU_APP_TOKEN": self.feishu.app_token,
            "FEISHU_TABLE_ID": self.feishu.table_id,
            "FEISHU_STATUS_FIELD": self.feishu.status_field,
            "FEISHU_STATUS_ACTIVE_VALUE": self.feishu.status_active,
            "FEISHU_STATUS_LEFT_VALUE": self.feishu.status_left,
            "WHITELIST_GROUPS": list(self.whitelist.groups),
            "MAX_RETRIES": self.retry.max_retries,
            "RETRY_DELAY": self.retry.delay,
            "ENABLE_PENDING_CHECK": self.pending.enabled,
            "PENDING_CHECK_INTERVAL": self.pending.interval,
            "ENABLE_STARTUP_REQUEST_SCAN": self.pending.startup_enabled,
            "STARTUP_REQUEST_SCAN_LIMIT": self.pending.startup_limit,
            "ENABLE_OFFLINE_NOTIFY": self.offline.enabled,
            "OFFLINE_FEISHU_TARGETS": list(self.offline.targets),
            "OFFLINE_FEISHU_ID_TYPE": self.offline.id_type,
            "OFFLINE_CHECK_INTERVAL": self.offline.check_interval,
            "OFFLINE_THRESHOLD": self.offline.threshold,
            "OFFLINE_RECOVERY_NOTIFY": self.offline.recovery_notify,
            "REQUEST_DELAY_MIN_SECONDS": self.delays.min_seconds,
            "REQUEST_DELAY_MAX_SECONDS": self.delays.max_seconds,
            "ENABLE_QQOFFICIAL_JOIN_POLL": self.poll.enabled,
            "QQOFFICIAL_POLL_INTERVAL": self.poll.interval,
            "QQOFFICIAL_POLL_LIMIT": self.poll.limit,
        }


__all__ = ["PluginConfig", "FeishuCfg", "PollCfg", "DelayCfg", "WhitelistCfg", "OfflineCfg", "PendingCfg", "RetryCfg", "_safe_int", "_safe_bool", "_safe_float"]
