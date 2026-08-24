"""
admissions_store — 深 module / 窄 interface / 高 locality

职责（depth）：
  收口原先散落在 ``main.BiliVerifyFeishuPlugin`` 与 ``storage`` 的
  入群状态与持久化细节，提供单一 seam 供整合任务串联。

隐藏的内部细节（seam 之后）：
  - data/whitelist.json / data/pending.json 路径
  - _atomic_write 原子写入
  - 内存缓存 + 文件的双重一致性
  - _pending_uid / _verified_before_join / _processed_request_keys 三集合

对外暴露的窄 interface（窄而稳定）：
  is_pending / remember_pending / mark_verified / discard_pending /
  dedup / enqueue_failed / list_pending / load_whitelist_cached / is_whitelisted

设计取舍（leverage / locality）：
  - leverage：复用 ``storage`` 的原子写入语义，但收口到本 module，避免
    main 直连文件系统；crash-safe 通过 mkstemp + os.replace 保证。
  - locality：所有去重与 pending 变更在同一 module 内完成，测试时可
    通过 ``memory_only=True`` 或自定义 ``data_dir`` 以内存 fake 替代文件，
    无需触及 main.py。
  - adapter 语义不在此 module，留给 PlatformPort；本 module 只做状态与持久化。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

try:  # AstrBot 环境取 logger，离线测试回退到 stdlib
    from astrbot.api import logger as _logger
except Exception:  # pragma: no cover
    import logging as _logging

    _logger = _logging.getLogger(__name__)

__all__ = ["AdmissionsStore"]


def _default_data_dir() -> Path:
    return Path(__file__).parent / "data"


class AdmissionsStore:
    """
    深 module：入群准入状态存储。

    interface:
      - is_pending(group, user) -> bool
      - remember_pending(group, user) -> None
      - mark_verified(group, user) -> None
      - discard_pending(group, user) -> None
      - dedup(join_request_id_key) -> bool
      - enqueue_failed(record: dict) -> None
      - list_pending() -> list[dict]
      - load_whitelist_cached(*, force_reload=False) -> list[str]
      - is_whitelisted(group) -> bool

    seam:
      内部隐藏文件路径与原子写入，外部仅通过上述 interface 交互。

    locality:
      去重 (_processed_request_keys) 与 pending 集合变更同处一 module，
      保证并发/崩溃场景下单一归口。

    测试 seam（depth 保留）：
      - ``memory_only=True`` 时完全走内存，不触文件系统
      - 或传入 ``data_dir`` / ``whitelist_file`` / ``pending_file`` 指向临时目录
    """

    # ------------------------------------------------------------------ #
    # 构造 / seam
    # ------------------------------------------------------------------ #
    def __init__(
        self,
        data_dir: Path | str | None = None,
        *,
        whitelist_file: Path | str | None = None,
        pending_file: Path | str | None = None,
        memory_only: bool = False,
    ) -> None:
        self._memory_only = bool(memory_only)

        if self._memory_only:
            # 纯内存 fake：路径置空，_mem_* 承载持久化语义
            self._data_dir: Path | None = None
            self._whitelist_file: Path | None = None
            self._pending_file: Path | None = None
            self._mem_whitelist: list[str] = []
            self._mem_pending: list[dict[str, Any]] = []
        else:
            if whitelist_file is not None or pending_file is not None:
                # 显式指定文件时，data_dir 仅用于 _ensure_data_dir 的兜底
                base = Path(data_dir) if data_dir is not None else _default_data_dir()
                self._data_dir = base
                self._whitelist_file = (
                    Path(whitelist_file)
                    if whitelist_file is not None
                    else base / "whitelist.json"
                )
                self._pending_file = (
                    Path(pending_file)
                    if pending_file is not None
                    else base / "pending.json"
                )
            else:
                base = Path(data_dir) if data_dir is not None else _default_data_dir()
                self._data_dir = base
                self._whitelist_file = base / "whitelist.json"
                self._pending_file = base / "pending.json"
            # 兼容已被外部预先传入的内存占位
            self._mem_whitelist = []  # type: ignore[assignment]
            self._mem_pending = []  # type: ignore[assignment]

        # 内存集合（不落盘，随进程生命周期）
        self._pending_uid: set[str] = set()
        self._verified_before_join: set[str] = set()
        self._processed_request_keys: set[str] = set()

        # 文件缓存（crash-safe 读取后常驻内存，写入时同步刷新）
        self._whitelist_cache: list[str] | None = None
        self._pending_cache: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------------ #
    # 内部：key 归一
    # ------------------------------------------------------------------ #
    @staticmethod
    def _key(group: str | int, user: str | int) -> str:
        return f"{str(group).strip()}:{str(user).strip()}"

    # ------------------------------------------------------------------ #
    # 内部：文件 seam（隐藏路径与原子写入）
    # ------------------------------------------------------------------ #
    def _ensure_data_dir(self) -> None:
        if self._memory_only or self._data_dir is None:
            return
        # 目录创建失败交由上层写入时报错，不在此处抛
        with contextlib.suppress(Exception):
            self._data_dir.mkdir(parents=True, exist_ok=True)

    def _atomic_write(self, filepath: Path, data: dict[str, Any]) -> None:
        """原子写入：mkstemp(dir=DATA_DIR) + os.replace，crash-safe。"""
        if self._memory_only:
            # 内存模式下由调用方直接更新 _mem_*，此处 no-op
            return
        self._ensure_data_dir()
        # 临时文件落在同目录，保证 rename 原子性（同文件系统）
        dir_for_tmp = (
            filepath.parent
            if filepath.parent.exists()
            else (self._data_dir or Path.cwd())
        )
        with contextlib.suppress(Exception):
            dir_for_tmp.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(dir_for_tmp), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, filepath)
        except Exception:
            if os.path.exists(tmp_path):
                with contextlib.suppress(Exception):
                    os.unlink(tmp_path)
            raise

    # ---- whitelist 文件 seam ----
    def _load_whitelist_from_file(self) -> list[str]:
        if self._memory_only:
            return list(self._mem_whitelist)
        assert self._whitelist_file is not None
        if not self._whitelist_file.exists():
            return []
        try:
            with open(self._whitelist_file, encoding="utf-8") as f:
                data = json.load(f)
            groups = data.get("groups", [])
            if isinstance(groups, list):
                return [str(g).strip() for g in groups if str(g).strip()]
            return []
        except Exception as e:
            _logger.error(f"读取白名单文件失败: {e}")
            return []

    def _save_whitelist_to_file(self, groups: list[str]) -> None:
        if self._memory_only:
            self._mem_whitelist = list(groups)
            return
        assert self._whitelist_file is not None
        self._atomic_write(self._whitelist_file, {"groups": groups})

    # ---- pending 文件 seam ----
    def _load_pending_from_file(self) -> list[dict[str, Any]]:
        if self._memory_only:
            return [dict(r) for r in self._mem_pending]
        assert self._pending_file is not None
        if not self._pending_file.exists():
            return []
        try:
            with open(self._pending_file, encoding="utf-8") as f:
                data = json.load(f)
            records = data.get("records", [])
            if isinstance(records, list):
                return [dict(r) for r in records if isinstance(r, dict)]
            return []
        except Exception as e:
            _logger.error(f"读取待处理队列文件失败: {e}")
            return []

    def _save_pending_to_file(self, records: list[dict[str, Any]]) -> None:
        if self._memory_only:
            self._mem_pending = [dict(r) for r in records]
            return
        assert self._pending_file is not None
        self._atomic_write(self._pending_file, {"records": records})

    # ------------------------------------------------------------------ #
    # interface：pending 集合
    # ------------------------------------------------------------------ #
    def is_pending(self, group: str | int, user: str | int) -> bool:
        """是否处于待补 UID 状态。"""
        return self._key(group, user) in self._pending_uid

    def remember_pending(self, group: str | int, user: str | int) -> None:
        """标记为待补 UID（幂等）。"""
        self._pending_uid.add(self._key(group, user))

    def mark_verified(self, group: str | int, user: str | int) -> None:
        """
        标记已在入群请求阶段完成 UID 校验。
        - 加入 _verified_before_join
        - 从 _pending_uid 移除（若存在）
        供 group_increase 到达时跳过二次待补。
        """
        k = self._key(group, user)
        self._verified_before_join.add(k)
        self._pending_uid.discard(k)

    def discard_pending(self, group: str | int, user: str | int) -> None:
        """清理 pending 与 verified 状态（退群/已处理后调用）。"""
        k = self._key(group, user)
        self._pending_uid.discard(k)
        self._verified_before_join.discard(k)

    # ------------------------------------------------------------------ #
    # interface：去重 seam
    # ------------------------------------------------------------------ #
    def dedup(self, join_request_id_key: str) -> bool:
        """
        去重：内部 _processed_request_keys。

        Returns:
          True  - 首次出现，已记录（可继续处理）
          False - 重复，已被处理过（应跳过）
        """
        key = str(join_request_id_key).strip()
        if not key:
            return False
        if key in self._processed_request_keys:
            return False
        self._processed_request_keys.add(key)
        return True

    # ------------------------------------------------------------------ #
    # interface：失败队列（文件+内存统一）
    # ------------------------------------------------------------------ #
    def enqueue_failed(self, record: dict[str, Any]) -> None:
        """
        将失败记录加入待处理队列。
        - 内存缓存与文件原子写入保持一致
        - record 为浅拷贝入队，避免外部篡改
        """
        if not isinstance(record, dict):
            raise TypeError("record must be dict")
        # 确保缓存已加载
        if self._pending_cache is None:
            self._pending_cache = self._load_pending_from_file()
        self._pending_cache.append(dict(record))
        self._save_pending_to_file(self._pending_cache)

    def list_pending(self) -> list[dict[str, Any]]:
        """
        返回待处理队列快照（拷贝），隐藏文件细节。
        首次调用触发懒加载，后续走内存缓存。
        """
        if self._pending_cache is None:
            self._pending_cache = self._load_pending_from_file()
        # 返回深一层拷贝，防止外部直接改内部缓存
        return [dict(r) for r in self._pending_cache]

    def clear_pending(self) -> None:
        """清空待处理队列（测试/运维用）。"""
        self._pending_cache = []
        self._save_pending_to_file([])

    # ------------------------------------------------------------------ #
    # interface：白名单（缓存+文件）
    # ------------------------------------------------------------------ #
    def load_whitelist_cached(self, *, force_reload: bool = False) -> list[str]:
        """
        加载白名单（带内存缓存）。
        - force_reload=False 时优先返回缓存
        - 隐藏 whitelist.json 路径与解析细节
        """
        if self._whitelist_cache is not None and not force_reload:
            return list(self._whitelist_cache)
        groups = self._load_whitelist_from_file()
        self._whitelist_cache = list(groups)
        return list(self._whitelist_cache)

    def is_whitelisted(self, group: str | int) -> bool:
        """快捷判断：群是否在白名单。

        白名单条目有两种合法形态（多 bot 场景下 qq_official 须为完整 UMO）：
          - 裸群号 / 群 openid：``1048195177``、``6CCC18AB...``
          - 完整 UMO：``default_1905473952:GroupMessage:6CCC18AB...``
        运行时事件/轮询传入的通常是裸 openid，因此除精确匹配外，
        还按条目末段（冒号最后一段）匹配，避免形态不一致导致永远判否。
        """
        gid = str(group).strip()
        if not gid:
            return False
        for entry in self.load_whitelist_cached():
            e = str(entry).strip()
            if gid == e:
                return True
            if ":" in e and e.rsplit(":", 1)[-1].strip() == gid:
                return True
        return False

    # ------------------------------------------------------------------ #
    # 额外 seam：供整合任务或测试使用的状态探针（不扩大 interface）
    # ------------------------------------------------------------------ #
    def _snapshot_memory(self) -> dict[str, Any]:  # pragma: no cover
        """测试探针：快照内存三集合（非 interface，仅调试）。"""
        return {
            "pending_uid": set(self._pending_uid),
            "verified_before_join": set(self._verified_before_join),
            "processed_keys": set(self._processed_request_keys),
        }

    def invalidate_cache(self) -> None:
        """失效文件缓存，下次读取重新走文件/内存 fake。"""
        self._whitelist_cache = None
        self._pending_cache = None
