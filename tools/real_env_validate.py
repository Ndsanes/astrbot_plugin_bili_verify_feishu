"""real_env_validate — 校验 .env / 环境变量中的业务配置是否齐全。

网关化改造后，认证、登录态、TAT 刷新与限速全部由 lark_cli 平台网关负责，
本插件不再自持凭据，本工具也只做本地配置校验，不发起真实写入。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

REQUIRED_KEYS = [
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate bili_verify local .env config (gateway mode)"
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to .env file (default: .env)",
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

    print("认证由 lark_cli 平台网关统一负责；如需验证链路，请在 AstrBot 实例内观察网关日志")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
