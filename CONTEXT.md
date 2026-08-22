# CONTEXT — 领域语言

本文件定义插件的领域术语，good seams 的命名来源。

- **Member** — 拟入群/已入群的 QQ 用户，以 openid / QQ 号标识，关联一个 B站 UID
- **Admission** — 入群审批决策域，输入为 JoinRequest（group + user + comment），输出为 Decision（approve/decline）与 Member 注册
- **MemberRegistry** — 飞书多维表格中的成员注册表，负责 UID/QQ 关联的持久化与状态（在群/已退群）
- **AdmissionsStore** — 入群状态的持久化与去重存储，管理 pending/verified/processed 集合与白名单
- **PlatformPort** — 平台端口，统一 OneBot 与 QQ 官方的入群申请列表与审批差异，后由两个 Adapter 实现
- **JoinRequest** — 归一后的入群申请 DTO（group_openid + member_openid + join_request_id + comment）
- **Config** — 插件配置的强类型视图，一次性解析与钳制

Seams：
- Admission ↔ MemberRegistry / AdmissionsStore / Config
- PlatformPort ↔ QqOfficialAdapter / OneBotAdapter
