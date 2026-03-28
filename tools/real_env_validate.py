import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

REQUIRED_KEYS = [
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_APP_TOKEN",
    "FEISHU_TABLE_ID",
]


def load_env_file(env_path: Path) -> dict[str, str]:
    """解析 .env 文件中的 KEY=VALUE 配置。"""
    result: dict[str, str] = {}
    if not env_path.exists():
        return result

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


def build_config(env_path: Path) -> dict[str, str]:
    """按优先级读取配置：系统环境变量 > .env 文件。"""
    file_env = load_env_file(env_path)
    merged = file_env.copy()
    merged.update(os.environ)
    return merged


def validate_required(config: dict[str, str]) -> list[str]:
    """检查必填项是否齐全。"""
    missing: list[str] = []
    for key in REQUIRED_KEYS:
        if not str(config.get(key, "")).strip():
            missing.append(key)
    return missing


def validate_format_warnings(config: Mapping[str, Any]) -> list[str]:
    """检查常见的配置格式问题，并返回告警信息。"""
    warnings: list[str] = []

    app_token = str(config.get("FEISHU_APP_TOKEN", "")).strip()
    table_id = str(config.get("FEISHU_TABLE_ID", "")).strip()

    if "://" in app_token:
        warnings.append("FEISHU_APP_TOKEN 看起来是完整 URL，建议仅填写 app_token")
    if "://" in table_id:
        warnings.append("FEISHU_TABLE_ID 看起来是完整 URL，建议仅填写 table_id")
    if "&" in table_id or "?" in table_id:
        warnings.append("FEISHU_TABLE_ID 含查询参数（如 &view=...），建议仅保留 table_id")

    return warnings


def make_fields(index: int) -> dict[str, str | int]:
    """构造一条用于验证的测试记录。"""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    uid = str(now_ms)[-10:] + f"{index:02d}"
    return {
        "UID": int(uid),
        "QQ号": 70000000 + index,
        "昵称": "env_validate",
        "时间": now_ms,
    }


async def run_write_validation(config: Mapping[str, Any], count: int) -> int:
    """执行真实写入验证，返回失败条数。"""
    try:
        from feishu_client import append_row_with_retry
    except ModuleNotFoundError as e:
        print(f"dependency missing: {e}")
        print(
            "please install required package in runtime environment, e.g. pip install lark-oapi"
        )
        return count

    failures = 0
    begin = time.perf_counter()

    for idx in range(count):
        fields = make_fields(idx)
        ok = await append_row_with_retry(fields=fields, config=config)
        if ok:
            print(f"[{idx + 1}/{count}] write ok, UID={fields['UID']}")
        else:
            failures += 1
            print(f"[{idx + 1}/{count}] write failed, UID={fields['UID']}")

    elapsed = time.perf_counter() - begin
    print(f"done: total={count}, failures={failures}, elapsed={elapsed:.2f}s")
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Feishu real environment by .env config"
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to .env file (default: .env)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Really write records to Feishu table",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=3,
        help="Number of records to write when --write is enabled (default: 3)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_path = Path(args.env_file)
    config = build_config(env_path)

    missing = validate_required(config)
    if missing:
        print("config invalid, missing keys:")
        for key in missing:
            print(f"- {key}")
        return 2

    print("config check passed")
    for warning in validate_format_warnings(config):
        print(f"config warning: {warning}")

    if not args.write:
        print("dry run mode: no records written")
        print("run with --write to execute real Feishu writes")
        return 0

    count = max(1, args.count)
    failures = asyncio.run(run_write_validation(config=config, count=count))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
