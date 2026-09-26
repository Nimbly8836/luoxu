# 独立群表存储（必须迁移）

现在每个 Telegram 群/频道有独立的普通消息表，不再存在统一的 `messages` 内容表，
也不再创建年份分区。不是“总消息表下面按群分区”，没有消息父表、继承或跨群 UNION 视图。
私聊使用独立表，**不是按群内发言人分表**。

## 表结构与路由

`message_archives` 保存 `(telegram_peer_type, telegram_peer_id)` 到内部 UUID 的映射：

```text
message_archives / conversations / 权限表：共享元数据
messages_<archive_uuid_hex>：该群或私聊的普通消息表
embeddings_<archive_uuid_hex>：对应的向量表（仅启用语义搜索时创建）
```

- 群的 Topics 共用群表，但保留 `conversation_id`、`topic_id` 和原有话题级授权。
- 私聊与同数字 ID 的群属于不同 archive；内部物理表名不使用用户传入的群名或数字。
- 消息表有固定 archive 的 CHECK，以及 `(conversation_id, archive_id)` 复合外键。
  将另一个群的会话写入本群表会被数据库拒绝。
- 每个表独立建立 PGroonga 全文、会话时间、回复及发送者索引。
- `message_template` 和可选的 `message_embedding_template` 是空的 DDL 模板，不存消息，
  不作为查询入口。`ensure_message_archive`/`provision_message_archive` 在事务中统一建表；
  并发创建同一群会串行化，不会产生两套表。
- `message_archives.table_version` 当前为 1。未来升级字段必须通过迁移同步修改模板和全部已建表，
  不能只改模板、只更新某个群，或手工改表名。
- 编辑/删除快照仍在已有 `message_revisions` 中，按会话索引和鉴权；默认关闭的历史开关不变。
- `archive_senders` 只保存各会话发送者 ID 和存活消息数，供头像权限校验使用，不包含文本或姓名。
  它随消息插入、编辑、软删除和物理删除同步维护；不会为头像请求扫描所有群表。

## API 的不兼容变更

`/search`（关键词、语义两种模式）和 `/names` **必须指定 `g` 或 `conversation_id`**：

```text
/search?g=123&q=关键词
/search?g=123&mode=semantic&q=工作压力&sender=456&exclude_sender=789,987
/search?conversation_id=<话题或私聊 UUID>&q=关键词
/names?g=123&q=昵称
/names?conversation_id=<会话 UUID>&q=昵称
```

- 未指定目标返回 400，不再默认跨群搜索。`sender` 不能代替目标会话。
- 无权限或不存在的目标返回 404；同时传两个参数但不属于同一 archive，也返回 404。
- 只传 `g` 要求整个群的访问权；只有话题授权时应传该话题的 `conversation_id`。
  同时附带匹配的 `g` 仍只查该话题，不扩大授权；没有父群权限时也不返回父群信息。
- 群级授权仍覆盖群及其 Topics；话题授权不扩散到其他话题。
- 全局群/会话列表与管理授权仍可用，只枚举共享元数据，不跨群查询内容。
- 上下文和回复链只读取目标所在的群表，并继续过滤不可访问的话题。
- 关键词搜索直接在所选群表按时间排序，不再逐年循环；时间、发送者和排除用户筛选保持有效。
- 前端不在此仓库，必须去掉“全部群搜索”入口，或要求先选群再发出请求。

## 全新数据库

执行新的 `dbsetup.sql` 即可，普通模式不需要 pgvector。
启用语义搜索时再执行 `migrations/005_semantic_search.sql` 并启动模型及 worker。
新群会按已启用的功能自动创建对应表；普通新增消息不会重复建表。
使用词云插件的源码部署还需重新编译并替换 `luoxu-cutwords`，旧二进制仍会访问已移除的总表。

## 已有数据库迁移

**这不是仅更新镜像即可完成的升级。** 先做可恢复的 PostgreSQL 备份，确保有足够空间暂存新旧数据及索引。
停止所有旧的 core/indexer、Python web 和 semantic-indexer；不要在旧程序仍写入时迁移。

1. 完成尚未应用的基础迁移 `001`—`004`。
2. 必须执行 `006_per_group_storage.sql`。
3. 若要启用语义搜索，且之前没有向量表，再执行新版 `005_semantic_search.sql`。
   **此处顺序是 006 → 可选 005**；005 不是所有部署必须执行的连续升级步骤。

```sh
# 保留你的项目名、配置、密码和现有 Compose -f 参数。
docker compose --profile core stop core
# 若另行运行 Python web / semantic-indexer，也必须停止它们。

docker compose exec -T db sh -c \
  'exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < migrations/006_per_group_storage.sql
```

外部数据库通过你的正常管理连接执行同一 SQL。迁移本身不需要模型服务在线。
006 在一个事务内锁定旧表，按 archive 复制所有年份的数据，并对完整行做双向差集校验。
原消息 ID、发送者、时间、文本、媒体、回复、编辑/删除状态均保留，授权和历史快照不扩散。
如果之前执行过旧版全局向量表的 005，006 也会复制并校验这些向量。

全部校验通过后才删除旧 `messages`（包括其年度分区）和旧全局 `message_embeddings`；
不使用 `CASCADE` 删除未知外部依赖。出错会回滚，必须处理错误后再启动新程序。
存在依赖旧消息表的自定义视图等对象时，迁移会失败，需要先备份并更新这些自定义对象。
成功记录 `per-peer-storage-v1` 标记，重复执行不重复复制数据。

新程序启动会检查迁移标记，未迁移不会正常启动。升级成功后，旧版本程序不再兼容此存储结构，
**回退需要恢复迁移前的完整备份，而不是只切回旧镜像**。
本仓库提供迁移代码，不会自动对运行中的数据库执行迁移。

## 检查迁移结果

```sql
SELECT id, telegram_peer_type, telegram_peer_id, table_version FROM message_archives;
SELECT name FROM bootstrap_state WHERE name = 'per-peer-storage-v1';
```

在 psql 中按 registry 生成受信任的只读检查语句（不要让客户端提供表名）：

```sql
SELECT format('SELECT %L AS archive, count(*) AS messages FROM %I;',
              id, 'messages_' || replace(id::text, '-', ''))
FROM message_archives ORDER BY id
\gexec
```

单群已停止监听时仍保留归档；删除监听引用不删表，也不改变消息可见权限。
需要整群清理时应先设计并执行匹配的消息、向量、计数和元数据清理流程，不要只手工 DROP 一张表。
