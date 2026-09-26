# 可选：语义搜索模式

默认仍是 PGroonga 关键词搜索。新增 `mode=semantic` 使用本地
`BAAI/bge-small-zh-v1.5` CPU 向量模型，按含义查找消息；不生成答案、不自动推断人物关系。
前端不在此仓库，需要自行添加模式切换，将这个参数传给已有 `/search` 接口。

## API

所有路径都需加上配置的 Web 前缀（示例 `/luoxu`），登录及授权方式不变：

```sh
curl -G http://localhost:9008/luoxu/search \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode 'g=123' \
  --data-urlencode 'mode=semantic' \
  --data-urlencode 'q=工作压力太大，想换工作' \
  --data-urlencode 'sender=123456' \
  --data-urlencode 'exclude_sender=789,987'
```

- `mode=keyword` 或不传：原有关键词搜索和时间排序。
- `mode=semantic`：`q` 必须是非空自然语言，最多 2000 字符。此时 `OR`、减号等不是关键词运算符。
- 必须指定 `g` 或 `conversation_id`，只查该群/私聊的独立表，不提供跨群搜索。
  未指定目标返回 400，无权限/不存在的目标返回 404。`sender`、`exclude_sender`、
  `start/end`（Unix 秒，开区间）仍生效，排除优先。
  姓名需先用同群的 `/names?g=...` 解析为 ID；不会从问题里自动提取姓名、时间。
- 结果按整个指定时间范围的余弦相似度排序，不按年份分批取最近消息。
- 每页最多 50 条，每条多一个 `score`（余弦相似度，不是置信概率）。
  `html` 是转义后的原文，不是模型生成内容，也不做关键词高亮。
- 语义响应另含 `mode: "semantic"` 和 `next_offset`。第一页 `offset=0`，之后使用
  返回的 `next_offset`；为 `null` 时 `has_more=false`。不要用最后一条的时间戳翻页。
  `offset` 最大 1000；翻页保持查询和过滤条件不变。归档或权限变化时页面可能移动。
- 普通模式不接受非零 `offset`。非法模式、空语义查询及不合法分页返回 400。
- 功能关闭、缺少迁移或模型服务暂不可用/繁忙时返回 503 和 `error`，不会偷偷降级为关键词搜索。
- 语义模式不索引或检索删除快照，`mode=semantic&include_deleted=true` 返回 400。需要搜索删除前正文时，使用[关键词删除快照搜索](deleted-message-search.md)。
- 两种模式均返回来源字段；语义结果始终是 `deleted=false`、`deleted_at=null`、`content_source=current`、`snapshot_captured_at=null`。

权限过滤在数据库排名/分页之前执行，管理员在内容接口也不绕过授权。
私聊、Topics 和已撤销授权的会话遵循现有访问规则，返回的消息可以继续调用上下文接口。

## 组件及配置

1. 主应用仍使用原来的轻量镜像，不安装 PyTorch。
2. `embeddings` 是独立 CPU 服务，默认 4 个推理线程；请求排队受限，忙时返回 503。
3. `semantic-indexer` 是独立后台进程，每轮为每个 archive 处理一批消息，避免大群独占回填。
   它从各群普通消息表读数据，写入同群向量表，持续处理新增和编辑。
4. PostgreSQL 需同时安装 PGroonga 和 pgvector。此版本针对约 10 万条消息使用精确向量排名，
   不使用可能在权限过滤后漏召回的近似索引；之后可按实测再优化。

在主应用和后台 worker 使用的 `config.toml` 中增加：

```toml
[database.semantic]
enabled = true
endpoint = "http://embeddings:8080/embed"
batch_size = 16       # 1-32；CPU 内存紧张时减小
poll_interval = 10    # 1-3600 秒；空闲/模型故障后的轮询间隔
```

容器外运行时将 endpoint 改为自己的受信任内网地址。服务收到归档文本和查询，
只应连接你控制的服务，不要填不可信外部地址。向量服务没有公开认证接口，
Compose **不映射宿主端口**，不要将其直接暴露到互联网。
首次启动需从 Hugging Face 下载固定 revision 的模型；缓存保存在 `luoxu-models` 卷。
推理不上传消息到 Hugging Face。文本先去掉首尾 Unicode 空白，纯空白消息跳过；
长消息再取前 8192 字符，并按模型上限截断到 512 token。
本版本逐条编码，不拼接别人的上下文。

## Docker 部署

使用 `docker-compose.yml` 加可选的 `docker-compose.semantic.yml`。
数据库覆盖镜像保持现有 PostgreSQL 17 和 PGroonga，并添加 pgvector。
**不要把正在使用其他 PostgreSQL 大版本的外部数据目录挂入这个镜像。**
语义模式不改变默认 CI 镜像构建；两个可选依赖镜像在本地单独构建。

以下以 `core` 为例，保持现有 `.env`、密码、项目名、卷和必要的其他 Compose 覆盖文件：

```sh
# 在本功能尚未发布到 GHCR 时，从当前源码构建应用；worker 复用同一镜像。
export LUOXU_IMAGE=luoxu:semantic-local
# POSTGRES_PASSWORD 仍由你现有的 .env 或环境变量提供。
docker compose -f docker-compose.yml -f docker-compose.semantic.yml \
  --profile core --profile semantic build core db embeddings
```

**已有数据库：先备份并停止所有写入进程/旧应用。** 不要删除数据卷。
确认 `001`–`004` 的基础迁移已经完成。再替换数据库容器，先执行必须的群表迁移 `006`，
再执行可选语义迁移 `005`；具体数据校验、锁定和回退条件见[独立群表存储](group-storage.md)：

```sh
docker compose -f docker-compose.yml -f docker-compose.semantic.yml \
  --profile core --profile semantic stop core semantic-indexer

docker compose -f docker-compose.yml -f docker-compose.semantic.yml \
  --profile core --profile semantic up -d --no-deps db

docker compose -f docker-compose.yml -f docker-compose.semantic.yml \
  exec -T db sh -c \
  'exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < migrations/006_per_group_storage.sql

docker compose -f docker-compose.yml -f docker-compose.semantic.yml \
  exec -T db sh -c \
  'exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < migrations/005_semantic_search.sql
```

等待数据库就绪后再执行 `psql`。外部数据库需由管理员安装 pgvector，并通过正常数据库连接执行
同一迁移。不要更换成只有 pgvector 而没有 PGroonga 的镜像。

**全新数据库：** 基础 Compose 导入 `dbsetup.sql`，语义覆盖文件自动追加导入 `005`。
未使用覆盖文件的普通新部署不要求 pgvector。已有卷不会自动重跑初始化 SQL。

配置 `enabled=true` 后启动：

```sh
docker compose -f docker-compose.yml -f docker-compose.semantic.yml \
  --profile core --profile semantic up -d --no-build

docker compose -f docker-compose.yml -f docker-compose.semantic.yml \
  logs -f semantic-indexer embeddings
```

仅 Web 部署将上述应用 profile/service `core` 换成 `web`，不要同时占用相同的 9008 端口。
已有新版发布镜像也可使用它作为 `LUOXU_IMAGE`，只需构建 `db embeddings`。
关闭功能时设置 `enabled=false` 并停止两个语义服务；已存向量不必删除，普通搜索照常运行。

## 索引生命周期与限制

worker 通过向量模型版本和当前消息内容摘要发现未完成的工作；重启自动续建，不依赖内存队列。
默认每批 16 条，模型请求在数据库事务之外执行，不阻塞 Telegram 写入。
写回前重新校验并短暂锁定原消息，防止把推理期间已修改/删除的旧内容写回。
数据库触发器会同步删除编辑/软删除消息的旧向量；物理删除通过外键级联清理。
搜索额外核对内容摘要并排除删除状态，未重建的编辑消息暂不参与语义搜索。
更改授权立即影响搜索结果，不需要重新计算向量。

- 初次回填和更新是异步的，结果只涵盖**已完成向量索引**的消息，不代表完整归档。
- worker 不采集/索引 `message_revisions`；历史快照开关和原有隐私边界保持不变。
- 模型名、revision、预处理版本及维度固定校验，不能直接将 endpoint 换成另一种模型。
- 相似度只表示候选相关性，不证明事实关联。短句、反讽、跨消息含义可能检索不好。
- 没有最低相似度阈值，靠后的结果可能不相关；请结合原文和消息上下文判断。

可在管理数据库连接中粗略查看进度（不要开放给普通内容 API；下列归档数量包含会被跳过的纯空白消息）：

```sql
-- 在 psql 中按 registry 中的内部 UUID 生成检查语句。
SELECT format('SELECT %L AS archive, count(*) AS embedded FROM %I;',
              id, 'embeddings_' || replace(id::text, '-', ''))
FROM message_archives ORDER BY id
\gexec
-- 消息数的逐群检查见 group-storage.md；旧模型向量也会计入这里的行数。
```

主应用源代码方式运行 worker：

```sh
python -m luoxu.semantic_indexer --config config.toml
# 回填到本轮无待处理消息即退出；服务故障会以失败状态退出。
python -m luoxu.semantic_indexer --config config.toml --once
```

## 回归验证

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt pyright
.venv/bin/python -m unittest discover -s tests -v
# 数据库测试仅使用一次性测试库，需提供 PGroonga、pgcrypto 和 pgvector：
LUOXU_TEST_DATABASE_URL=postgresql://... .venv/bin/python -m unittest discover -s tests -v
```

语义单元测试不下载模型；数据库集成测试使用可预测的向量验证真实 SQL、权限、过滤、排序、
分页、编辑/删除与推理竞态。真实模型的效果需用自己的查询集评估。
