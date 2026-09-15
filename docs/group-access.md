# 监听群组、公开群组与账号授权

监听与访问权限是两件事。管理员可先添加手动监听，再决定哪些账号能查看归档；现有的个人或公开授权也会维持所属群的采集需求，但不会扩大任何人的阅读权限。

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

旧版本可能在历史任务启动后因 `KeyError: 'id'` 返回 500；这不代表群没有写入数据库。升级后可先查询管理列表，再按需授权，无须删除已有归档或清空加载进度。当前持久化监听版本需要先执行下文的 `004` 迁移。

该接口需要索引进程提供 Telegram 客户端及监控回调；仅 Web 模式没有该能力时返回 503。Telegram 账号必须能够访问目标群，本接口不会自动加入群。

**监听已持久化：** 成功添加会保存手动引用，重启后即使没有给任何账号授权，也会恢复采集需求。`telegram.index_groups` 只在首次启动时导入一次；之后改配置列表不会增减监听，旧配置也不会把后台停用的群重新启用。新增用 POST，已有归档用 PUT/DELETE 管理手动引用。

### 2. 查看所有群与公开状态

```http
GET /api/luoxu/admin/conversations
GET /api/luoxu/admin/groups
GET /api/luoxu/admin/public
```

`/admin/conversations` 返回全部归档（`conversations` 数组），包括已停止的群、话题和私聊；`/admin/groups` 只返回引用数大于零的规范群（`groups` 数组），**不是运行成功保证**；`/admin/public` 返回显式公开授权。父群公开时，话题有效公开，但不一定存在话题的单独公开授权。

群和其话题的管理列表条目还包含所属群的 `monitoring`：

```json
{
  "requested": true,
  "manual": false,
  "account_references": 2,
  "public_references": 0,
  "reference_count": 2,
  "runtime": {"state": "running", "error_type": null}
}
```

- `manual`：是否保留独立的手动监听引用；最多计 1。
- `account_references/public_references`：该群及话题上的显式授权条数，不是去重后的账号数，也不统计权限继承副本。
- `reference_count`：以上三种来源之和；`requested` 表示是否大于零。
- `runtime`：当前索引进程的观察值。`starting/pending` 是等待启动，`running` 是已挂接实时订阅（不代表历史全部下载完成），`retrying` 是失败等待重试，`stopping/stopped` 是停止中/已停止。失败仅暴露异常类型 `error_type`，详情看服务日志。
- 独立 Python Web 没有本地索引器时为 `unknown`；没有 `monitoring` 的私聊不使用这套群监听机制。

索引器约每 2 秒重新读取持久化引用，能接收独立 Web 进程的变更。数据库读取失败不会被当成“没有引用”；已有采集继续，稍后重试。Telegram 无法解析或无访问权时保留监听意图并重试，不伪称已经运行。

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

### 5. 停止或恢复手动监听

```http
DELETE /api/luoxu/admin/groups/<group_conversation_uuid>
PUT /api/luoxu/admin/groups/<group_conversation_uuid>
```

两者成功返回 204，可重复调用，只接受规范父群 UUID；话题、私聊或不存在的群返回 404。独立 Web 也能修改已有群的手动引用，运行中的索引器随后轮询接收；没有运行的索引器就不会实际采集。

DELETE **只删除手动引用**，不是删除群、消息或授权。即使 `manual=false`，只要还有任一个人或公开授权，群仍会采集。给话题授权也维持所属群的采集，但不会让该账号读取整个群。禁用账号保留其授权与引用；删除账号会级联移除其授权。

最后一个引用消失后，索引器移除 New/Edit/Delete 订阅并取消、等待历史下载及已进入的事件处理；归档消息、修订、UUID 和已保存的历史游标不删除。再次启用复用同一归档并从已保存进度继续。停用/离线期间的编辑、删除不能由 Telegram 历史接口完整重建。每个索引进程中每群只有一个工作任务；本功能不是跨多个索引进程的分布式主节点选举。

### 6. 从旧版本升级

1. 备份数据库，停止旧索引器和 Python Web 服务。
2. 按顺序执行尚未应用的 `001` 至 `004`；若已完成 `001`—`003`，只执行 `migrations/004_group_monitoring.sql`。不要重导 `dbsetup.sql` 覆盖已有数据库。
3. `004` 按已确认的“保留全部旧群监听”策略，**一次性**为所有旧 `tg_groups` 登记的规范群补手动引用；不改变公开或个人权限。旧版本无法区分动态监听和仅归档群，因此不再需要的旧群也可能恢复，需要逐个取消手动引用。
4. 启动新版服务，检查管理列表的引用和运行状态。不要通过删除会话或清空数据库来停用监听。

`group-monitoring-legacy-v1` 标记旧数据接管完成；`group-monitoring-config-v1` 单独标记配置导入完成。初始配置导入不会覆盖已有停用记录。迁移重复执行不会重新启用停用记录，也不会接管之后新建的仅归档群。全新 `dbsetup.sql` 已标记无需旧数据接管。

## 权限与监听回归

`tests/test_admin_groups.py` 使用真实 aiohttp 服务、账号登录/JWT、Indexer 添加入口和隔离 PostgreSQL schema；Telegram 网络被替换为测试桩，不访问真实群。多数测试替换历史工作以控制时序，另有测试运行真实历史索引器并在 Telegram I/O 边界验证取消。

回归包含 A、B、匿名三种身份在群列表、会话列表、全局/指定群/指定会话搜索、成员名、消息详情、UUID/旧版上下文及头像上的可见范围，也验证撤销权限后旧 token 和已热身头像不能绕过检查。

```sh
LUOXU_TEST_DATABASE_URL='postgresql://USER:PASSWORD@HOST/DISPOSABLE_TEST_DB' \
  python -m unittest discover -s tests -v
```

监听回归还覆盖重新创建索引器恢复、最后一个引用移除与原归档续用、多人/公开/话题引用、最后账号删除、初次配置导入与停用记录、真实迁移重复执行以及独立 Web 变更的轮询接收。

必须使用可丢弃的 PostgreSQL/PGroonga 测试库，不要对生产库执行测试。
