"""白名单配置播种回归测试（v0.1.3）。

历史 bug：``_init_whitelist_from_config`` 定义后从未被任何路径调用；repo 方式
更新插件时会以仓库内容覆盖插件目录，运行时 ``data/whitelist.json`` 随之丢失，
此后每次启动白名单恒为空，QQ 官方入群申请轮询静默空转、加群申请无人处理。
回归点：
1. 播种行为本身：空文件播种 / 已有持久化数据不覆盖 / 配置为空不落盘 /
   逗号分隔字符串兼容形态；
2. ``initialize()`` 必须实际调用播种（源码级接线守卫，防再次退化为死代码）。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from astrbot_plugin_bili_verify_feishu import storage
from astrbot_plugin_bili_verify_feishu.main import BiliVerifyFeishuPlugin

OPENID = "6CCC18AB28098F241B44FF1A41F6668F"


@pytest.fixture()
def whitelist_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把白名单存储整体重定向到临时目录（含原子写临时文件目录）。"""
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "WHITELIST_FILE", tmp_path / "whitelist.json")
    return tmp_path / "whitelist.json"


def _make_plugin(conf: dict) -> BiliVerifyFeishuPlugin:
    """绕过 __init__ 构造最小实例：仅注入 AstrBotConfig 形态的配置 dict。"""
    plugin = BiliVerifyFeishuPlugin.__new__(BiliVerifyFeishuPlugin)
    plugin.config = conf
    return plugin


def _read_groups(whitelist_file: Path) -> list[str]:
    return json.loads(whitelist_file.read_text(encoding="utf-8")).get("groups", [])


def test_文件缺失时从配置播种(whitelist_file: Path):
    umo = f"default_1905473952:GroupMessage:{OPENID}"
    _make_plugin({"WHITELIST_GROUPS": [umo]})._init_whitelist_from_config()
    assert _read_groups(whitelist_file) == [umo]


def test_已有持久化数据不覆盖(whitelist_file: Path):
    whitelist_file.write_text(
        json.dumps({"groups": ["g-exist"]}, ensure_ascii=False), encoding="utf-8"
    )
    _make_plugin({"WHITELIST_GROUPS": ["g-config"]})._init_whitelist_from_config()
    assert _read_groups(whitelist_file) == ["g-exist"]


def test_配置为空时不落盘(whitelist_file: Path):
    _make_plugin({"WHITELIST_GROUPS": []})._init_whitelist_from_config()
    assert not whitelist_file.exists()


def test_逗号分隔字符串配置兼容(whitelist_file: Path):
    _make_plugin({"WHITELIST_GROUPS": "a, b,,c"})._init_whitelist_from_config()
    assert _read_groups(whitelist_file) == ["a", "b", "c"]


def test_initialize_接线守卫():
    """播种必须在 initialize 中被真实调用，而非只定义不接线。"""
    source = inspect.getsource(BiliVerifyFeishuPlugin.initialize)
    assert "_init_whitelist_from_config()" in source
