# AstrBot 插件 — QQ入群请求到飞书表格自动登记与放行

## What（做什么）

实现一个 AstrBot 插件，当收到 QQ 入群请求时：
1. 检查群聊是否在白名单中
2. 从请求消息中提取纯数字 UID（B站用户ID）
3. 将 UID 和 QQ 号,及当前群号写入飞书多维表格的指定表
4. 通过该入群请求

## Why（为什么做）

- **自动化流程**：减少人工审核入群请求的工作量
- **数据记录**：将入群用户信息自动同步到飞书表格，便于管理和追溯
- **白名单控制**：只对指定群组启用此功能，避免误操作
- **提升效率**：实现入群请求的自动处理和放行

## How（怎么做）

### 技术架构

```
┌─────────────────────────────────────────────────────────────┐
│                    AstrBot 插件系统                          │
├─────────────────────────────────────────────────────────────┤
│  main.py (事件处理器)                                        │
│    ├─ 监听入群请求事件                                        │
│    ├─ 调用白名单检查                                          │
│    ├─ 提取 UID                                               │
│    ├─ 调用飞书客户端写入表格                                   │
│    └─ 返回放行结果                                            │
├─────────────────────────────────────────────────────────────┤
│  plugins/                                                   │
│    ├─ config.py (配置加载)                                   │
│    ├─ feishu_client.py (飞书异步客户端)                       │
│    └─ storage.py (白名单持久化)                              │
├─────────────────────────────────────────────────────────────┤
│  data/ (持久化数据目录)                                       │
│    ├─ config.yaml (配置文件)                                 │
│    ├─ whitelist.json (群白名单)                              │
│    └─ pending.json (失败写入队列)                             │
└─────────────────────────────────────────────────────────────┘
```

### 实现步骤

#### 步骤 1：创建配置与凭据加载模块

**文件**: `plugins/config.py`

**功能**:
- 支持从环境变量读取配置（优先级高）
- 支持从 `data/config.yaml` 读取配置（回退）
- 提供配置项验证和默认值

**配置项**:
```yaml
# 飞书应用凭据
FEISHU_APP_ID: "cli_xxxxxxxxxxxx"
FEISHU_APP_SECRET: "xxxxxxxxxxxxxxxxxxxxxxxx"

# 飞书多维表格配置
FEISHU_APP_TOKEN: "appbcbWCzen6D8dezhoCH2RpMAh"  # 多维表格的 app_token
FEISHU_TABLE_ID: "tblxxxxxxxxxxxx"

# 白名单群号列表
WHITELIST_GROUPS:
  - "123456789"
  - "987654321"

# 重试配置
MAX_RETRIES: 3
RETRY_DELAY: 1  # 秒
```

**实现要点**:
- 使用 `os.environ.get()` 读取环境变量
- 使用 `yaml.safe_load()` 读取 YAML 文件
- 提供 `get_config(key, default)` 接口
- 配置项类型转换和验证

---

#### 步骤 2：实现 Feishu 异步客户端模块

**文件**: `plugins/feishu_client.py`

**功能**:
- `append_row_to_table(app_token, table_id, row_data)`: 向多维表格追加行

**技术方案**:
- 使用官方 `lark-oapi` SDK（`pip install lark-oapi`）
- SDK 自动管理 `tenant_access_token` 的获取和刷新
- 使用 SDK 的异步方法（方法名加 `a` 前缀，如 `acreate`）

**SDK 初始化**:
```python
import lark_oapi as lark
from lark_oapi.api.bitable.v1 import *

client = lark.Client.builder() \
    .app_id("APP_ID") \
    .app_secret("APP_SECRET") \
    .log_level(lark.LogLevel.DEBUG) \
    .build()
```

**API 调用流程**:
```
1. 构建请求对象
   CreateAppTableRecordRequest.builder() \
       .app_token("app_token") \
       .table_id("table_id") \
       .request_body(CreateAppTableRecordRequestBody.builder()
           .fields({"UID": "12345", "QQ号": "67890"})
           .build()) \
       .build()

2. 发起异步请求
   response = await client.bitable.v1.app_table_record.acreate(request)
```

**实现要点**:
- 使用 `lark-oapi` SDK 内置的异步能力
- 请求失败重试机制（指数退避）
- 完整的错误处理和日志记录

---

#### 步骤 3：实现白名单持久化与访问接口

**文件**: `plugins/storage.py`

**功能**:
- `load_whitelist()`: 加载白名单
- `save_whitelist()`: 保存白名单
- `is_group_whitelisted(group_id)`: 检查群是否在白名单
- `add_group_to_whitelist(group_id)`: 添加群到白名单
- `remove_group_from_whitelist(group_id)`: 从白名单移除群
- `load_pending()`: 加载待处理队列
- `save_pending()`: 保存待处理队列
- `add_to_pending(data)`: 添加到待处理队列

**数据结构**:
```json
// data/whitelist.json
{
  "groups": ["123456789", "987654321"]
}

// data/pending.json
{
  "records": [
    {
      "timestamp": "2024-01-01T00:00:00Z",
      "group_id": "123456789",
      "user_id": "67890",
      "uid": "12345",
      "retry_count": 0
    }
  ]
}
```

**实现要点**:
- 使用 JSON 格式存储（简单易用）
- 文件读写使用异步操作
- 提供原子性写入（先写临时文件，再重命名）
- 白名单变更时自动保存

---

#### 步骤 4：在 main.py 中注册事件处理器

**文件**: `main.py`

**功能**:
- 监听入群请求事件
- 实现完整的处理流程
- 提供管理命令（添加/移除白名单）

**事件处理流程**:
```
收到入群请求
    ↓
检查群是否在白名单
    ↓ (否) 忽略
    ↓ (是)
从消息中提取 UID (正则 \d+)
    ↓ (未找到) 提示用户输入 UID
    ↓ (找到)
调用飞书客户端写入表格
    ↓ (成功) 返回放行结果
    ↓ (失败) 重试或加入待处理队列
```

**管理命令**:
- `/verify add_group <group_id>` - 添加群到白名单
- `/verify remove_group <group_id>` - 从白名单移除群
- `/verify list_groups` - 列出白名单群
- `/verify retry_pending` - 重试待处理队列

**实现要点**:
- 使用 `@filter.event` 装饰器监听事件
- 使用 `@filter.command` 装饰器注册命令
- 异步处理所有操作
- 完整的错误处理和用户反馈

---

#### 步骤 5：实现错误处理与重试机制

**集成到**: `plugins/feishu_client.py` 和 `main.py`

**功能**:
- 飞书 API 调用失败时自动重试
- 指数退避策略（1s, 2s, 4s, ...）
- 超过最大重试次数后加入待处理队列
- 待处理队列定期重试（可配置）

**重试策略**:
```python
async def retry_with_backoff(func, max_retries=3, base_delay=1):
    for attempt in range(max_retries):
        try:
            return await func()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            await asyncio.sleep(delay)
```

---

#### 步骤 6：更新文档与示例配置

**文件**:
- `README.md` - 更新插件说明和使用指南
- `data/config.yaml.example` - 示例配置文件
- `metadata.yaml` - 更新插件元数据

**文档内容**:
- 插件功能说明
- 安装和配置步骤
- 环境变量配置说明
- 使用示例
- 常见问题解答

---

## 文件清单

| 文件 | 类型 | 说明 |
|------|------|------|
| `main.py` | 修改 | 插件主入口，注册事件处理器 |
| `plugins/__init__.py` | 新增 | Python 包初始化文件 |
| `plugins/config.py` | 新增 | 配置加载模块 |
| `plugins/feishu_client.py` | 新增 | 飞书异步客户端 |
| `plugins/storage.py` | 新增 | 白名单持久化接口 |
| `data/config.yaml.example` | 新增 | 示例配置文件 |
| `data/whitelist.json` | 运行时生成 | 群白名单数据 |
| `data/pending.json` | 运行时生成 | 失败写入队列 |
| `README.md` | 修改 | 更新插件文档 |
| `metadata.yaml` | 修改 | 更新插件元数据 |

## 依赖项

- `lark-oapi` - 飞书官方 Python SDK（内置异步支持、自动 token 管理）
- `pyyaml` - YAML 文件解析

## 验证方案

### 单元测试
- 测试配置加载（环境变量和文件）
- 测试白名单操作（添加、移除、检查）
- 测试 UID 提取正则表达式
- Mock 飞书客户端测试写入逻辑

### 集成测试
- 使用测试飞书应用和表格
- 模拟入群请求事件
- 验证表格新增行
- 验证放行结果

### 手动测试
```bash
# 模拟入群请求
python main.py --simulate join_event.json

# 检查配置
python -c "from plugins.config import get_config; print(get_config('FEISHU_APP_ID'))"

# 测试飞书连接
python -c "from plugins.feishu_client import get_tenant_access_token; import asyncio; asyncio.run(get_tenant_access_token())"
```

## 安全考虑

1. **凭据安全**
   - 不在代码中硬编码凭据
   - 使用环境变量或配置文件
   - 配置文件不提交到版本控制

2. **输入验证**
   - 验证群号格式
   - 验证 UID 格式（纯数字）
   - 防止注入攻击

3. **错误处理**
   - 不暴露敏感信息到日志
   - 优雅处理所有异常
   - 失败时不泄露内部状态

## 后续扩展

1. **B站用户验证**（可选）
   - 调用 B站 API 验证用户是否存在[否定]
   - 检查用户粉丝数、等级等[否定]

2. **白名单管理命令**
   - 在群内动态管理白名单[否定]
   - 权限控制（仅管理员可用）[否定，astrbot 提供了后台管理页面，不需要在群内管理]

3. **数据统计**[否定，可以通过飞书表格自有统计功能实现，无需重复开发]
   - 入群请求统计
   - 成功率统计
   - 图表展示

4. **多表格支持**
   - 不同群使用不同表格[否定，不同群可以存在于同一表格中，通过群号字段区分]
   - 表格映射配置
