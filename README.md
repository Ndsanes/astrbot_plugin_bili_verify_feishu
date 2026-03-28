# astrbot_plugin_bili_verify_feishu

QQ 群入群校验辅助插件：在白名单群内，记录新成员提供的 B 站 UID，并写入飞书多维表格。

## 配置方式

本插件使用 AstrBot 官方插件配置机制，所以你可以在 AstrBot WebUI 插件配置页面直接填写以下配置项：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_APP_TOKEN`
- `FEISHU_TABLE_ID`
- `WHITELIST_GROUPS`
- `MAX_RETRIES`
- `RETRY_DELAY`
- `ENABLE_PENDING_CHECK`
- `ENABLE_STARTUP_REQUEST_SCAN`
- `STARTUP_REQUEST_SCAN_LIMIT`
- `PENDING_CHECK_INTERVAL`

## 白名单行为

- 运行期白名单持久化在 `data/whitelist.json`。
- 如果持久化白名单为空，插件会在首次初始化时用配置项 `WHITELIST_GROUPS` 进行初始化。
- 后续白名单维护建议直接在 AstrBot WebUI 插件配置中修改。

## 定时巡检

- 通过 `ENABLE_PENDING_CHECK` 可一键开关巡检（默认开启）。
- 插件会按 `PENDING_CHECK_INTERVAL`（默认 `3800` 秒）定时检查白名单群。
- 巡检内容包括：
	- 已入群但尚未提供 UID 的成员（内存中的待处理集合）。
	- 飞书写入失败后落盘在 `data/pending.json` 的待处理记录。
- 若发现未处理数据，会输出告警日志，便于管理员及时跟进。

## 启动补偿扫描

- 通过 `ENABLE_STARTUP_REQUEST_SCAN` 可开启/关闭启动补偿扫描（默认开启）。
- 插件加载时会调用 OneBot `get_group_system_msg`，尝试处理插件加载前未处理的加群请求。
- 单次最多处理 `STARTUP_REQUEST_SCAN_LIMIT` 条（默认 `50`），防止历史积压影响启动速度。

## 真实环境验证（.env）

可通过本仓库提供的验证脚本做一次真实链路验证（飞书写入）。

1. 在项目根目录创建 `.env`，至少包含：
	- `FEISHU_APP_ID`
	- `FEISHU_APP_SECRET`
	- `FEISHU_APP_TOKEN`
	- `FEISHU_TABLE_ID`
2. 可选配置：
	- `MAX_RETRIES`（默认 `3`）
	- `RETRY_DELAY`（默认 `1.0`）
3. 先做配置体检（不写入飞书）：
	- `python3 tools/real_env_validate.py`
4. 执行真实写入验证（示例写入 3 条）：
	- `python3 tools/real_env_validate.py --write --count 3`

说明：插件内已统一做飞书 API 调用限速，单进程下最多 `5 req/s`，用于降低触发 Lark 限速的风险。

## 参考文档

- AstrBot 插件配置文档：https://docs.astrbot.app/dev/star/guides/plugin-config.html
