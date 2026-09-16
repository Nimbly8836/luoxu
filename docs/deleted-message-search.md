# 搜索已删除消息

搜索接口新增可选参数 `include_deleted`，**默认 `false`**。不传或显式传 `false` 时，仍只搜索当前未删除正文。

```http
GET /api/luoxu/search?q=关键词&include_deleted=true
GET /api/luoxu/search?conversation_id=<uuid>&include_deleted=true
```

前缀以实际配置为准。参数接受 `true` / `false`（不区分大小写）；空值、`1`、`0`、`yes` 等返回 400。`true` 表示**包含正常消息和已删除消息**，不是只查询已删除消息。

## 开启条件与权限

```toml
[web.message_history]
enabled = true
```

- 收集删除快照的 core 和提供 API 的 Python Web 均需使用正确配置；独立运行时分别加载配置并重启。开关默认关闭。
- Web 或存储层未启用历史功能时，`include_deleted=true` 返回 400，不读取既存快照。普通搜索仍可使用。
- 只返回当前身份有权访问的会话。个人授权、公开授权及父群到话题的权限继承保持不变；话题授权不扩大到父群或其他话题。
- 管理员没有内容权限绕过；私聊仍需个人授权。撤权后，旧 token 的后续请求也不能读取快照。
- **公开会话的删除快照在开启本功能后，也可被匿名用户显式检索。** 不希望匿名读取历史内容的会话，不应授予公开访问。
- 返回快照的搜索响应使用 `Cache-Control: private, no-store` 和 `Vary: Authorization`。

开启配置不能补录之前未捕获的删除内容，也不会访问 Telegram 或触发回填。

## 查询语义

开启此选项后：

1. 正常消息仍使用当前正文。
2. 已删除消息只使用对应归档记录的最新 **delete 快照**，不使用 edit 快照，也不使用删除记录里可能残留的正文。
3. 没有删除快照时，只有无关键词查询才能返回该删除记录，`html=null`；它不能按不存在的旧正文命中关键词。

快照按会话 UUID、消息 ID 和原消息创建时间匹配，不会从另一会话或另一年份的同号消息借用正文。若同一记录保留多份删除快照，先按捕获时间、再按快照 ID 选择最新一份。

`q` 的匹配与高亮使用同一份正文；正文中的 HTML 被转义，关键词由 PGroonga 高亮。`g`、`conversation_id`、`sender`、`exclude_sender`、`start`、`end` 对两类结果都生效。发送者排除优先于包含条件，未知发送者仍遵循原规则。

日期过滤和排序使用**原消息创建时间**，不是删除时间。正常和删除结果合并后，按原有创建时间、消息 ID 倒序，使用相同的 50 条搜索上限和 `has_more` 语义；已删除消息也占用该上限。

这是“当前消息 + 显式删除快照”搜索，不是任意编辑历史全文搜索。给 `/search` 传 `include_history=true` 不能替代此参数。

## 返回字段

保留原有 `groupinfo`、`has_more`、`messages` 及消息字段，并为每条搜索结果补充：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `deleted` | boolean | 是否已删除 |
| `deleted_at` | number / null | 删除记录时间，Unix 秒；未删除为 null |
| `content_source` | string | `current`、`delete_snapshot` 或 `unavailable` |
| `snapshot_captured_at` | number / null | 使用的删除快照捕获时间，Unix 秒；其他情况为 null |

`html` 保持原有 HTML 字符串格式，但无可用删除快照时允许为 null。`t` 仍为原消息时间，`edited` 仍为已归档的最后编辑时间。

客户端应同时显示删除状态和快照来源，不能把快照伪装成仍然存在的 Telegram 消息。选择消息后访问普通详情或任一上下文入口，已删除正文仍返回 null；搜索选项不会改变这些接口。

## 升级与限制

此功能只读既有消息和历史表，**没有新增 SQL 迁移**，不自动开启历史采集，不修改归档、授权或监听状态。原有安装仍需满足既有迁移要求。

默认搜索保留原来的当前正文查询路径。显式包含删除消息时，会额外查询历史表；既有会话/消息快照索引可用于查找，但没有专门的删除快照全文索引，也没有生产数据性能基准，不能承诺与普通搜索同等速度。

`tests/test_search_deleted.py` 使用隔离 PostgreSQL/PGroonga 和真实 HTTP 请求验证默认行为、显式开关、快照/高亮、跨年物理记录、缺失快照、筛选与合并上限、账号/公开/话题/私聊权限及即时撤权；不连接 Telegram 或生产数据库。
