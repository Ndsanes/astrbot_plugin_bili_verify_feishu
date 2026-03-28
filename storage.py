import json
import os
import tempfile
from pathlib import Path

from astrbot.api import logger

DATA_DIR = Path(__file__).parent / "data"
WHITELIST_FILE = DATA_DIR / "whitelist.json"
PENDING_FILE = DATA_DIR / "pending.json"


def _ensure_data_dir() -> None:
    """确保 data 目录存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write(filepath: Path, data: dict) -> None:
    """原子性写入 JSON 文件（先写临时文件，再重命名）。"""
    _ensure_data_dir()
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, filepath)
    except Exception:
        # 清理临时文件
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


# ---- 白名单操作 ----


def load_whitelist() -> list[str]:
    """加载白名单群号列表。"""
    if not WHITELIST_FILE.exists():
        return []
    try:
        with open(WHITELIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("groups", [])
    except Exception as e:
        logger.error(f"读取白名单文件失败: {e}")
        return []


def save_whitelist(groups: list[str]) -> None:
    """保存白名单群号列表。"""
    _atomic_write(WHITELIST_FILE, {"groups": groups})


def is_group_whitelisted(group_id: str) -> bool:
    """检查群是否在白名单中。"""
    whitelist = load_whitelist()
    return group_id in whitelist


def add_group_to_whitelist(group_id: str) -> bool:
    """添加群到白名单，已存在返回 False。"""
    whitelist = load_whitelist()
    if group_id in whitelist:
        return False
    whitelist.append(group_id)
    save_whitelist(whitelist)
    return True


def remove_group_from_whitelist(group_id: str) -> bool:
    """从白名单移除群，不存在返回 False。"""
    whitelist = load_whitelist()
    if group_id not in whitelist:
        return False
    whitelist.remove(group_id)
    save_whitelist(whitelist)
    return True


# ---- 待处理队列操作 ----


def load_pending() -> list[dict]:
    """加载待处理队列。"""
    if not PENDING_FILE.exists():
        return []
    try:
        with open(PENDING_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("records", [])
    except Exception as e:
        logger.error(f"读取待处理队列文件失败: {e}")
        return []


def save_pending(records: list[dict]) -> None:
    """保存待处理队列。"""
    _atomic_write(PENDING_FILE, {"records": records})


def add_to_pending(record: dict) -> None:
    """添加记录到待处理队列。"""
    records = load_pending()
    records.append(record)
    save_pending(records)


def clear_pending() -> None:
    """清空待处理队列。"""
    save_pending([])
