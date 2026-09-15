# 消息上下文与整段讨论

两个 HTTP 入口返回相同结构，且**只查当前身份可访问的本地数据库，不向 Telegram 补抓**：

```http
GET /api/luoxu/context?g=1998301990&id=200
GET /api/luoxu/conversations/<conversation_uuid>/messages/200/context
```

前缀以实际配置为准。目标未收录或当前身份无权限，均返回 404；登录不代表能访问任意群，管理员也没有普通内容接口的权限绕过。

## 窗口与回复讨论不同

- `target`：选中的消息。
- `before` / `after`：同一会话中的相邻消息；话题窗口不会混入其他话题。这些消息不一定有回复关系。
- `replies`：先沿当前消息向前查引用原文，再从目标及已找到的原文向后展开，包含后续多层回复和同一原文下的平行分支。原文不用命中本次搜索，也不必在 `before/after` 或同一年内。

例如选择 B：

```text
A 原文
├── B 当前选中
│   └── D 后续回复 B
└── C 平行回复 A
    └── E 后续回复 C
```

权限、深度和条数允许时，`target` 是 B，`replies` 包括 A、C、D、E，而不只是 D。实际内容使用完整的归档 `text`（以及转义后的 `html`）；`quote_text` 只是 Telegram 引用片段，不能代替原文。

`replies` 保持祖先由近到远的顺序，之后按层展开后续分支，同层按消息 ID 排序。它不是全局时间排序，也不是嵌套树。前端应根据 `reply_to_id` 建立关系，根据每条消息的 `conversation_id` 打开详情，不要假定全部属于目标的话题。

目标不会在 `replies` 重复出现；讨论按同一 Telegram peer 内的消息 ID 去重，也会终止循环引用。正常消息使用 `id`；不可用占位保留兼容字段 `msgid`。

## 参数与界限

| 查询参数 | 默认值 | 含义 |
| --- | --- | --- |
| `before` | 5 | 前面的相邻消息数 |
| `after` | 5 | 后面的相邻消息数 |
| `depth` | 5 | 向前的祖先跳数，然后从目标及祖先链向后展开的层数 |
| `reply_limit` | 100 | `replies` 总条数上限，包括不可用占位，不包括 target/before/after |

请求可以缩小范围；超过服务端 `[web.context]` 上限会被截到配置值，负数或非整数返回 400。`before + after` 仍受 `max_window` 限制，超过它返回 400。

`depth` 不是整条路径共享的距离预算：先最多向前 N 层，再从找到的祖先链和目标向后 N 层。层数内可能有大量分支，因此还受 `reply_limit` 控制。设为 0 会省略相应展开；如果仍有已知未返回的引用或可访问分支，会明确标记截断。当前没有续页游标。

只看讨论、不取相邻消息：

```http
GET /api/luoxu/context?g=1998301990&id=200&before=0&after=0&depth=5&reply_limit=100
```

服务端设置：

```toml
[web.context]
before = 5
after = 5
max_window = 20
reply_depth = 5
reply_limit = 100
```

## 必须检查 replies_meta

上下文新增元数据；原有四个字段继续保留：

```json
{
  "scope": "accessible_local_archive",
  "complete": false,
  "truncated": true,
  "unavailable_count": 0,
  "deleted_count": 0,
  "depth": 5,
  "limit": 100
}
```

- `scope`：只有当前身份可访问的本地归档，不是 Telegram 的完整历史。
- `truncated`：条数/深度界限导致已知引用或可访问分支未全部返回；不会统计或披露隐藏分支。
- `unavailable_count`：本次返回的不可用引用占位数。
- `deleted_count`：目标加回复讨论中已删除的消息数，不计无关的 before/after 邻居。
- `complete`：没有截断、不可用占位或已删除内容时为 true。它**不保证未采集或隐藏的消息不存在**。
- `depth/limit`：服务端最终应用的值，不一定等于客户端提交的值。

原文缺失或不可访问时，统一返回不可区分的占位：

```json
{"msgid": 100, "status": "unavailable"}
```

不返回隐藏原文的正文、会话 UUID 或隐藏分支计数。已可见消息暴露的引用 ID 可用于连接其他有权限的平行分支，但不会据此获得原文权限。若祖先原文不可用，就无法知道它更早引用了谁。

已删除消息保留关系与删除标记，`text/html` 为 null，不能把旧正文当作当前原文返回。可选历史快照仍由单独的 `/history` 接口、配置开关和权限控制。

## 验证范围

`tests/test_topics.py` 在隔离的真实 PostgreSQL/PGroonga schema 中经两个 HTTP 入口验证：原文和分支不受窗口/搜索影响、跨年记录、深度/条数及零值界限、循环去重、缺失/删除原文、话题授权及即时撤权、同号但不同 peer 类型的隔离。不连接 Telegram，也不对生产库执行。
