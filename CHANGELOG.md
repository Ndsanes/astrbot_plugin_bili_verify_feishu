# 更新日志

本文件供 AstrBot 面板「插件更新日志」读取（`CHANGELOG.md`）。每次发版前更新对应条目。

## v0.1.3 (2026-08-25)

### 修复
- **【严重】白名单配置播种从未接线，repo 更新后轮询永久空转**：`_init_whitelist_from_config()` 自 v0.0.x 起定义后没有任何调用点。白名单持久化文件位于插件目录内（`data/whitelist.json`），repo 方式更新会以仓库内容覆盖整个插件目录、运行时文件随之清空；此后每次启动 `load_whitelist()` 恒为空列表，QQ 官方入群申请轮询在 `_poll_qqofficial_join_requests_once` 入口静默返回——日志只有「开始首次拉取」，之后无任何动作也无告警（2026-08-25 实例实测复现）。现于 `initialize()` 开头接通播种：文件缺失/为空时自动从 `WHITELIST_GROUPS` 配置（存于实例 cmd_config.json，不受插件目录覆盖影响）重建白名单，已有数据不覆盖。回归测试 `tests/test_whitelist_seeding.py` 含 initialize 接线守卫。
  - 已知残留：`pending.json` 等其余运行时状态同样存于插件目录内，仍会在 repo 更新时丢失；后续可考虑迁移至 `data/plugin_data/` 持久化根。

## v0.1.2 (2026-08-24)

### 清理

- 移除已弃用的配置键 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`（v0.1.0 网关化改造后认证全部由 lark_cli 平台网关负责，键保留仅为兼容旧配置）：`_conf_schema.json` 不再展示、`FeishuCfg.app_id/app_secret` 字段与 `from_dict`/`get`/`as_dict` 中的对应条目一并删除。已填写的旧值会被忽略，无需处理。
- `FEISHU_APP_TOKEN` / `FEISHU_TABLE_ID`（业务数据归属）不变；bot 身份经 gateway.api() 的多维表格读写链路未改动。

## v0.1.1 (2026-08-24)

### 清理

- 删除从未接线的深模块化重构残留：`platform_port.py` 仅保留 `JoinRequest` DTO（移除 PlatformPort 协议与 QqOfficialAdapter / OneBotAdapter 适配器）、`admission.admit_message`、`MemberRegistry.mark_left/find/from_plugin_config`、`storage.add_group_to_whitelist/remove_group_from_whitelist/clear_pending`、main 的 `_platform_port` 占位与 `_uid_pattern` 死正则。
- 移除幽灵配置键 `FEISHU_QQ_FIELD` 读取；删除过时设计文档 `plan.md` 与 `tests/stubs/lark_oapi` 残留桩目录。
- **【行为修正】** 未处理入群请求巡检间隔兜底默认值从 60s 统一为 3800s（与 `_conf_schema.json` / README 对齐，经 `plugin_config.PENDING_CHECK_INTERVAL_DEFAULT` 单点共享）。
- `AdmissionsStore.clear_pending`（有测试覆盖的状态机方法）不受影响；bot 身份经 gateway.api() 的多维表格读写链路未改动。

## v0.1.0 (2026-08-24)

### 变更

- **飞书调用收口到 lark_cli 平台网关**：`feishu_client` 内部引擎从 lark-oapi SDK 整体替换为经 `lark_cli` 平台适配器透传（多维表格 records search/create/update、im/v1/messages）。认证、登录态、TAT 刷新与限速全部由网关单点负责；插件在 initialize 时自动注入网关，未注入或调用抛错时记 ERROR 并走既有失败语义（False/None），不崩溃。
- 移除 lark-oapi 依赖（requirements.txt 清空）与本地 5 QPS 限速器；`FEISHU_APP_ID` / `FEISHU_APP_SECRET` 不再必填（配置键保留但已弃用），`FEISHU_APP_TOKEN` / `FEISHU_TABLE_ID` 仍由本插件配置（业务数据归属）。
- 重试/指数退避等业务韧性逻辑保留；测试改为注入假网关（记录 method/path/data 并回预设响应），删除 lark_oapi 桩包。

## v0.0.7 (2026-08-24)

### 修复

- **【严重】白名单 UMO 形态与运行时不匹配**：v0.0.5 多 bot 改造把 qq_official 白名单条目改为完整 UMO（`实例ID:GroupMessage:群openid`），轮询侧学会了拆 UMO 定位归属 bot，但 `AdmissionsStore.is_whitelisted` 仍是裸 openid 精确匹配——轮询拉到的每一条入群申请都因「非白名单群忽略」被静默丢弃。现除精确匹配外按条目末段（冒号最后一段）匹配，两种形态均可命中。

## v0.0.6 (2026-08-24)

### 修复

- **【严重】入群申请去重语义反转**：`admit_request` 把 `AdmissionsStore.dedup` 的"首次出现"返回值误当"重复命中"，导致每个新 `join_request_id` 的第一次申请**不经过 UID 校验/白名单判断即被放行**，重复的第二次申请反而走完整流程。现已按 store 语义反转分支：首次走完整审批（UID 校验 → 飞书登记），重复申请返回 `skip` 不做任何平台动作。
- **成功路径 verified 标记被误清**：UID 登记成功后先 `mark_verified` 又调 `discard_pending`，后者会把 `_verified_before_join` 一并清除，使进群事件（group_increase）无法跳过二次待补。现只调 `mark_verified`（其本身已移除 pending）。

### 测试

- 新增领域测试 42 项：Decision 分支（approve/decline/skip）、AdmissionsStore 状态机与去重、白名单 UMO 归一化、11255 重探计时。

## v0.0.5 (2026-08-23)

### 修复

- **qq_official 多 bot 凭据错配（严重）**：实例同时配置多个 qq_official 机器人时，入群申请轮询与审批固定使用第一个注册实例的凭据。对不属于该 bot 的群，官方网关返回 `11255「请求的资源不存在(用户/群已注销)」`，插件据此把活跃群误判为已注销并永久跳过轮询。现已按白名单条目 UMO 首段（平台实例 ID，如 `default_1905473952`）精确路由到归属 bot。

### 变更

- 白名单条目须为完整 UMO：`平台实例ID:消息类型:群openid`（例：`default_1905473952:GroupMessage:6CCC18AB28098F241B44FF1A41F6668F`）。纯 group_openid / 数字群号无法定位归属实例，将被跳过并记录日志。
- 「bot 与群无关联」（11255，通常为 bot 被移出群或未获管理员授权）不再永久拉黑：改为带时间戳标记，30 分钟后自动重探，兼容 bot 后续进群 / 获授权的场景。
- 审批接口复用轮询记录的群归属实例，确保放行 / 拒绝由正确的 bot 执行；归属未记录时明确报错而不误发请求。

## v0.0.4 (2026-08-22)

- feat(qq_official): 主动轮询入群申请列表并按 UID 自动审批
- fix(qq_official): 轮询过滤无效白名单项（数字群号 / UMO 全串归一化），避免对已注销群反复报错

## v0.0.3 (2026-08-21)

- feat: 适配 QQ 官方机器人 websocket（qq_official），双平台兼容

## v0.0.1 - v0.0.2

- 初始版本：aiocqhttp 入群请求监听、B站 UID 校验、飞书多维表格登记
