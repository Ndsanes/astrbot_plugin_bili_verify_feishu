"""白名单条目 UMO 归一化测试——main.BiliVerifyFeishuPlugin._split_umo_entry。

@staticmethod 可直接调用；main.py 的导入依赖由 tests/stubs 桩包满足（见 conftest）。
"""

from __future__ import annotations

from astrbot_plugin_bili_verify_feishu.main import BiliVerifyFeishuPlugin

OPENID = "6CCC18AB28098F241B44FF1A41F6668F"
split = BiliVerifyFeishuPlugin._split_umo_entry


def test_完整UMO取openid与首段平台实例():
    entry = "default_1905473952:GroupMessage:6CCC18AB28098F241B44FF1A41F6668F"
    assert split(entry) == (OPENID, "default_1905473952")


def test_同openid不同instance解析出不同归属():
    a = split("default_111:GroupMessage:" + OPENID)
    b = split("default_222:GroupMessage:" + OPENID)
    assert a == (OPENID, "default_111")
    assert b == (OPENID, "default_222")
    # 多 bot 场景：归属差异完全体现在第二返回值
    assert a[0] == b[0] and a[1] != b[1]


def test_四段以上UMO首段与末段():
    # 多余中间段不影响"首段=实例、末段=群标识"的约定
    assert split("default_1:X:Y:" + OPENID) == (OPENID, "default_1")


def test_纯数字群号跳过():
    """aiocqhttp 数字群号是 OneBot 语义，官方接口不可用 → ("", "")。"""
    assert split("1048195177") == ("", "")
    assert split(" 1048195177 ") == ("", "")


def test_格式错误输入():
    assert split("") == ("", "")
    assert split(None) == ("", "")  # type: ignore[arg-type]
    assert split(":") == ("", "")
    assert split("default_1:") == ("", "")


def test_空首段openid仍可解析():
    # 空首段：platform_id 为 ""，openid 仍可解析（调用方因 pid 空跳过）
    assert split(":GroupMessage:" + OPENID) == (OPENID, "")


def test_openid长度边界():
    # 官方 openid 区间 [8, 128]，越界一律拒绝
    assert split("1234567") == ("", "")  # 7 位过短（且全数字）
    assert split("a" * 7) == ("", "")
    assert split("a" * 129) == ("", "")
    assert split("a" * 128) == ("a" * 128, "")
    assert split("a" * 8) == ("a" * 8, "")
