# 普通回复误分类为 Topic 的修复

## 原因与新行为

Telegram 的 `reply_to_top_id` 既用于论坛话题，也用于普通回复串；它本身不能证明群启用了 Topics。旧版把带此字段的回复都分配到新的 `topic` 会话，因此普通群会出现大量以群名命名的假话题。

修复后只在频道/超级群消息的回复头明确带有 `forum_topic` 标记时识别话题。直接回复话题根消息时，如果没有 `reply_to_top_id`，使用 `reply_to_msg_id` 作为话题 ID。普通回复保留 `reply_to_id`，但不再设置 `topic_id` 或新建话题会话。

## 已收录数据的修复

索引器初始化群时会获取 Telegram 群资料。只有列入 `telegram.repair_non_forum_groups` 的群才允许修复，默认不搬移已有数据；还必须是完整的普通群实体，或者明确没有 `forum` / `monoforum` 标记且不是 `min` 精简实体的频道/超级群，才会在数据库事务中归并已有假话题：

- 保留原群 UUID、旧版 `group_id`、现有群授权及加载进度。
- 将假话题消息移回原群，清空消息级 `topic_id`，保留回复、引用、媒体以及编辑/删除状态。
- 同一消息已经在原群或多个假话题中出现时合并重复记录：删除状态优先，其次保留较新的状态，不让旧回放恢复已删除消息。
- 将已捕获的历史记录移回原群；即使当前关闭历史采集，也不会删除这些记录。修复本身不额外采集历史快照。
- 移除假话题会话及其直接用户/公开授权，**不将话题授权升级为整个群的授权**。管理员如需恢复这类用户的访问，应明确授权原群。
- 真实话题群、单话题频道及资料不完整的实体会跳过。修复失败时事务回滚；重复初始化不会再次搬移已经修好的记录。

Telegram 当前的群属性无法证明群过去是否曾启用 Topics，因此修复名单必须由管理员确认。不要把曾经使用真实话题的群加入名单。此次修复不改变数据库表结构，也不能仅凭数据库中一排同名话题判断是否应该归并，因此没有提供无条件删除 `topic` 的 SQL 迁移。

## 升级步骤

1. 备份 PostgreSQL 数据库，停止仍使用旧版代码的索引器，避免旧进程继续创建假话题。
2. 更新主程序/主镜像，同时更新独立部署的 Web 服务。
3. 在现有 `[telegram]` 配置节中加入 `repair_non_forum_groups = [1998301990]`（整数 ID，不加引号），并确保目标群位于 `telegram.index_groups` 中，然后重启 `core` 索引器。若群仅通过管理 API 动态添加，重启后需要重新添加以触发群初始化；单独重启 `web` 不会执行数据修复。
4. 检查索引器日志中的 `reply-thread conversations into non-forum group`，确认对应群已处理。大型群归并涉及消息和索引写入，请预留维护时间与磁盘空间。
5. 通过 `/conversations` 或 `/admin/conversations` 确认原群仍存在、假话题消失，再验证搜索和上下文。必要时重新配置被移除的假话题专属授权。完成后可移除 `repair_non_forum_groups` 配置；新消息识别修复不依赖这个开关。

以下上下文入口均需加上部署的 API 前缀，并遵守相同的访问权限：

```text
/context?g=1998301990&id=402868
/conversations/{conversation_id}/messages/402868/context
```

前者可定位该群内有权限访问的消息，包括真正的话题消息；响应与 UUID 入口相同。消息尚未收录或当前身份无权访问时仍然返回 404。接口只使用本地归档，不会即时请求 Telegram。

## 回归测试

不依赖 pytest：

```sh
python -m unittest discover -s tests -v
```

要同时运行数据库和 HTTP 测试，请使用**独立的测试 PostgreSQL/PGroonga 实例**，不要使用生产数据库：

```sh
LUOXU_TEST_DATABASE_URL='postgresql://USER:PASSWORD@HOST/TEST_DB' \
  python -m unittest discover -s tests -v
```

测试为每个用例建立并清理独立 schema。覆盖普通回复/真实话题识别、跨年份消息搬移、历史保留、重复记录、删除优先、事务回滚、话题专属授权，以及旧式上下文 URL 与 UUID URL 的响应一致性。
