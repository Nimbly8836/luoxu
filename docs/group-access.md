# 监听群组、公开群组与账号授权

监听与访问权限是两件事。管理员决定索引哪些群，再决定哪些账号能查看归档。

## 权限规则

| 身份 | 普通内容接口可见范围 | 管理能力 |
| --- | --- | --- |
| 匿名访问者 | 明确对本站公开的群及其话题 | 无 |
| 普通登录用户 | 公开群 + 分配给自己的群及其话题；私聊须单独授权 | 无 |
| 管理员 | 内容接口同样按公开权限与个人授权检查 | 可列出全部归档会话、添加监听、管理用户和授权 |

- **添加监听不会自动公开群，也不会自动给任何账号添加授权。**
- **需要按账号区分的群不要设为公开。** 登录用户继承公开权限；撤销某人的个人授权，不会覆盖公开权限。系统没有“公开但禁止某个账号”的拒绝列表。
- 管理员通过 `/admin/conversations` 查看全部会话，这是管理列表，不是普通用户的可见列表。若管理员也需要从普通搜索接口阅读非公开群，可给自己的账号授权。
- Telegram 群有公开用户名或邀请链接，不代表其 Luoxu 归档已公开。`pub_id` 是 Telegram 用户名，不是本站权限标志。
- 管理列表和添加监听的响应提供 `is_public`，表示当前匿名身份能否访问该会话。公开父群下的话题也会显示 `true`。
- 群授权继承到其话题；只授权某个话题，不会扩大为整个群的权限。私聊必须显式配置索引并逐账号授权，不能公开。
- 授权存储在数据库中。变更后，已有登录 token 的下一次请求就会重新检查权限，无须重新登录。已经下载到客户端的内容无法撤回。

## 管理操作

下面以配置的前缀 `/api/luoxu` 为例。所有 `/admin/*` 请求均携带管理员登录取得的：

```http
Authorization: Bearer <admin_access_token>
```

匿名调用管理接口返回 401，普通用户调用返回 403。不要让普通用户共用管理员账号或 token。

### 1. 添加监听

```http
POST /api/luoxu/admin/groups
Content-Type: application/json

{"group":"-1001234567890"}
```

成功返回 **201**，包括完整会话及权限标志：

```json
{
  "conversation": {
    "id": "11111111-1111-4111-8111-111111111111",
    "kind": "group",
    "name": "待授权群组",
    "telegram_peer_type": "channel",
    "telegram_peer_id": 1234567890,
    "topic_id": null,
    "pub_id": null,
    "legacy_group_id": 1234567890,
    "is_public": false
  }
}
```

授权接口使用返回的 **`conversation.id` UUID**，不是 Telegram 群号。已经监听的群会返回现有会话，不重置授权，也不重新启动历史任务。

旧版本可能在历史任务启动后因 `KeyError: 'id'` 返回 500；这不代表群没有写入数据库。升级后可先查询管理列表，再按需授权，无须删除已有归档或清空加载进度。本次修复不需要 SQL 迁移。

该接口需要索引进程提供 Telegram 客户端及监控回调；仅 Web 模式没有该能力时返回 503。Telegram 账号必须能够访问目标群，本接口不会自动加入群。

**现有运行限制：** 动态添加的监听状态保存在当前索引进程内；重启后自动恢复监听仍需将群加入 `telegram.index_groups`，或重新调用添加接口。数据库中的归档和访问授权不会因此清空。本次响应与权限修复未改变监听的持久化机制。

### 2. 查看所有群与公开状态

```http
GET /api/luoxu/admin/conversations
GET /api/luoxu/admin/public
```

第一个返回全部会话及 `is_public`；第二个返回显式公开授权的会话。两者对话题可能不同：父群公开时，子话题有效公开，但不一定存在子话题的单独公开授权。

普通客户端应使用 `/groups` 或 `/conversations`，它们只返回当前身份有权限的记录，不能使用管理员列表代替。

### 3. 创建普通账号并分别授权

```http
POST /api/luoxu/admin/users
Content-Type: application/json

{"username":"user_a","password":"<独立的强密码，至少8字符>","is_admin":false}
```

从响应取得用户 UUID，授予指定会话：

```http
PUT /api/luoxu/admin/users/<user_a_uuid>/grants/<conversation_uuid>
GET /api/luoxu/admin/users/<user_a_uuid>/grants
```

`PUT` 成功返回 204，重复授权安全；`GET` 返回该账号的显式授权。给 A 授权不会让 B 获得同样权限。

撤销：

```http
DELETE /api/luoxu/admin/users/<user_a_uuid>/grants/<conversation_uuid>
```

撤销该条授权后，如果仍有公开授权或父群授权，用户依然可能有访问权；需要同时检查授权来源。

### 4. 仅在确实需要时公开

```http
POST /api/luoxu/admin/public/<conversation_uuid>
```

成功返回 204，此后匿名及所有登录用户可访问该群。取消公开使用：

```http
DELETE /api/luoxu/admin/public/<conversation_uuid>
```

取消公开不删除消息，也不撤销已存在的个人授权。如果现在所有账号都看到同一批群，先检查它们是否已经公开，而不是仅撤销个人授权。

## 权限回归

`tests/test_admin_groups.py` 使用真实 aiohttp 服务、账号登录/JWT、Indexer 添加入口和隔离 PostgreSQL schema；Telegram 网络与历史下载被替换为测试桩，不访问真实群。

回归包含 A、B、匿名三种身份在群列表、会话列表、全局/指定群/指定会话搜索、成员名、消息详情、UUID/旧版上下文及头像上的可见范围，也验证撤销权限后旧 token 和已热身头像不能绕过检查。

```sh
LUOXU_TEST_DATABASE_URL='postgresql://USER:PASSWORD@HOST/DISPOSABLE_TEST_DB' \
  python -m unittest discover -s tests -v
```

必须使用可丢弃的 PostgreSQL/PGroonga 测试库，不要对生产库执行测试。
