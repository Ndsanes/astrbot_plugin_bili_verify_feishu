"""AdmissionsStore 状态机测试。

覆盖：pending/verified 状态跃迁、dedup 去重语义、失败队列（文件+内存）、
白名单持久化 roundtrip（tmp_path 数据目录）。全部零网络、零第三方依赖。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrbot_plugin_bili_verify_feishu.admissions_store import AdmissionsStore

# --------------------------------------------------------------------------- #
# pending / verified 状态机
# --------------------------------------------------------------------------- #


def test_pending_状态机_remember_is_mark_verified(tmp_path: Path):
    store = AdmissionsStore(data_dir=tmp_path)
    assert not store.is_pending("g1", "u1")

    store.remember_pending("g1", "u1")
    assert store.is_pending("g1", "u1")
    # 幂等：重复 remember 不报错不变化
    store.remember_pending("g1", "u1")
    assert store.is_pending("g1", "u1")

    # mark_verified：进入 verified 集合并移出 pending（供 group_increase 跳过二次待补）
    store.mark_verified("g1", "u1")
    assert not store.is_pending("g1", "u1")
    snap = store._snapshot_memory()
    assert "g1:u1" in snap["verified_before_join"]

    # discard_pending：pending 与 verified 一并清理
    store.discard_pending("g1", "u1")
    snap = store._snapshot_memory()
    assert "g1:u1" not in snap["verified_before_join"]
    assert "g1:u1" not in snap["pending_uid"]


def test_key_归一化_数字与字符串等价(tmp_path: Path):
    store = AdmissionsStore(data_dir=tmp_path)
    store.remember_pending(12345, 67890)
    assert store.is_pending("12345 ", " 67890")  # str+strip 后同一 key


# --------------------------------------------------------------------------- #
# dedup 去重 seam
# --------------------------------------------------------------------------- #


def test_dedup_首次True再次False(tmp_path: Path):
    store = AdmissionsStore(data_dir=tmp_path)
    key = "group:user:req-1"
    assert store.dedup(key) is True  # 首次出现，已记录
    assert store.dedup(key) is False  # 重复，应跳过


def test_dedup_空key返回False且不污染集合(tmp_path: Path):
    store = AdmissionsStore(data_dir=tmp_path)
    assert store.dedup("") is False
    assert store.dedup("   ") is False
    assert store._snapshot_memory()["processed_keys"] == set()


def test_dedup_key先strip再去重(tmp_path: Path):
    store = AdmissionsStore(data_dir=tmp_path)
    assert store.dedup(" k1 ") is True
    assert store.dedup("k1") is False


# --------------------------------------------------------------------------- #
# 失败队列（enqueue_failed / list_pending / clear_pending）
# --------------------------------------------------------------------------- #


def test_enqueue_list_clear_roundtrip_内存(tmp_path: Path):
    store = AdmissionsStore(data_dir=tmp_path)
    record = {"uid": "123456789", "user_id": "u9"}
    store.enqueue_failed(record)
    # record 为浅拷贝入队，外部篡改不影响队列
    record["uid"] = "hacked"
    pending = store.list_pending()
    assert len(pending) == 1 and pending[0]["uid"] == "123456789"

    # list_pending 返回快照拷贝，修改返回值不触达内部缓存
    pending[0]["uid"] = "mutated"
    assert store.list_pending()[0]["uid"] == "123456789"

    store.clear_pending()
    assert store.list_pending() == []


def test_enqueue_failed_非dict抛TypeError(tmp_path: Path):
    store = AdmissionsStore(data_dir=tmp_path)
    with pytest.raises(TypeError):
        store.enqueue_failed(["not-a-dict"])  # type: ignore[arg-type]


def test_failed_queue_文件持久化_roundtrip(tmp_path: Path):
    """store1 入队 → 新实例从同一 data_dir 读出，验证原子写盘可恢复。"""
    store1 = AdmissionsStore(data_dir=tmp_path)
    store1.enqueue_failed({"uid": "100200300", "retry_count": 0})
    store1.enqueue_failed({"uid": "100200301", "retry_count": 2})

    store2 = AdmissionsStore(data_dir=tmp_path)
    assert [r["uid"] for r in store2.list_pending()] == ["100200300", "100200301"]

    # clear_pending 同样落盘
    store2.clear_pending()
    assert AdmissionsStore(data_dir=tmp_path).list_pending() == []
    assert (tmp_path / "pending.json").exists()


def test_memory_only_模式不落盘(tmp_path: Path):
    store = AdmissionsStore(memory_only=True)
    store.enqueue_failed({"uid": "1"})
    store.remember_pending("g", "u")
    assert len(store.list_pending()) == 1
    # 内存模式路径为 None，不可能产生任何文件；目录保持为空即为证明
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- #
# 白名单（缓存 + 文件持久化）
# --------------------------------------------------------------------------- #


def _write_whitelist(tmp_path: Path, groups: list[str]) -> None:
    (tmp_path / "whitelist.json").write_text(
        json.dumps({"groups": groups}, ensure_ascii=False), encoding="utf-8"
    )


_UMO = "default_appid:GroupMessage:6CCC18AB28098F241B44FF1A41F6668F"


def test_whitelist_persistence_roundtrip(tmp_path: Path):
    _write_whitelist(tmp_path, [_UMO, " g2 "])
    store = AdmissionsStore(data_dir=tmp_path)

    loaded = store.load_whitelist_cached()
    # 条目被 strip，UMO 全串原样保留（归属解析是上层职责）
    assert loaded[0] == _UMO
    assert loaded[1] == "g2"
    assert store.is_whitelisted("g2") is True
    assert store.is_whitelisted("g3") is False
    assert store.is_whitelisted("") is False  # 空群号恒 False
    assert store.is_whitelisted("  g2 ") is True  # 归一化后命中


def test_whitelist_cache_外部改文件需force_reload(tmp_path: Path):
    _write_whitelist(tmp_path, ["g1"])
    store = AdmissionsStore(data_dir=tmp_path)
    assert store.is_whitelisted("g1") is True

    # 外部直接改文件：缓存未失效前读到旧值
    _write_whitelist(tmp_path, ["g1", "g2"])
    assert store.is_whitelisted("g2") is False

    # force_reload / invalidate_cache 两条路都能看到新数据
    assert store.load_whitelist_cached(force_reload=True) == ["g1", "g2"]
    _write_whitelist(tmp_path, ["g9"])
    store.invalidate_cache()
    assert store.load_whitelist_cached() == ["g9"]
    assert store.is_whitelisted("g1") is False


def test_whitelist_文件缺失与坏JSON容错(tmp_path: Path):
    store = AdmissionsStore(data_dir=tmp_path)
    assert store.load_whitelist_cached() == []  # 文件不存在 → 空

    (tmp_path / "whitelist.json").write_text("{broken json", encoding="utf-8")
    store.invalidate_cache()
    assert store.load_whitelist_cached() == []  # 坏 JSON 容错为空列表


def test_whitelist_非list的groups字段容错(tmp_path: Path):
    (tmp_path / "whitelist.json").write_text(json.dumps({"groups": "oops"}), encoding="utf-8")
    store = AdmissionsStore(data_dir=tmp_path)
    assert store.load_whitelist_cached() == []
