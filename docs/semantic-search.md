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
- 向量召回按整个指定时间范围的余弦相似度排序，不按年份分批取最近消息。
  可选独立 CPU 重排服务会重新排列固定候选窗口，再按相关性阈值过滤。
- 纯向量模式每页最多 50 条，`score` 是余弦相似度；重排默认每页最多 20 条，
  `score` 是 sigmoid(logit) 的 0–1 **相关性分数，不是校准后的正确性概率**。
  重排同时返回原始余弦 `vector_score`。
  `html` 是转义后的原文，不是模型生成内容，也不做关键词高亮。
- 语义响应另含 `mode: "semantic"`、`ranking: "vector" | "reranker"`、`min_score`
  （有效阈值；纯向量为 null）、`page_size`、`candidates`（固定候选上限；纯向量为 null）
  和 `next_offset`。第一页 `offset=0`，之后使用
  返回的 `next_offset`；为 `null` 时 `has_more=false`。不要用最后一条的时间戳翻页。
  `offset` 最大 1000；翻页保持查询和过滤条件不变。归档或权限变化时页面可能移动。
- 重排时可传 `min_score=0.5` 覆盖本次阈值（有限数值，0–1，包含边界）；低于阈值不返回，
  不强行填满页面。关键词模式或未启用重排时传此参数返回 400。
- 普通模式不接受非零 `offset`。非法模式、空语义查询及不合法分页返回 400。
- 功能关闭、缺少迁移或模型服务暂不可用/繁忙时返回 503 和 `error`，不会偷偷降级为关键词搜索。
- 语义模式不索引或检索删除快照，`mode=semantic&include_deleted=true` 返回 400。需要搜索删除前正文时，使用[关键词删除快照搜索](deleted-message-search.md)。
- 两种模式均返回来源字段；语义结果始终是 `deleted=false`、`deleted_at=null`、`content_source=current`、`snapshot_captured_at=null`。

权限及所有元数据过滤在数据库候选排名/分页之前执行，管理员在内容接口也不绕过授权。
重排请求不持有数据库事务或锁；推理后总会重新检查授权和路由（即使窗口为空或全部低分），
仅按初始候选的完整物理键 `(conversation_id, msgid, created_at)` 回查至多 `candidates` 条，
不重新扫描向量排名、不读取向量载荷或计算距离。回查验证当前元数据过滤、删除状态、模型及内容摘要，
只保留正文未变的已评分消息，返回新鲜元数据和初始 `vector_score`。编辑、删除、撤权及发送者过滤变化
不会把旧的已评分正文返回；新的未评分消息不用于补齐。
本次请求以初始已评分窗口为准：推理期间新增的更优向量不会挤掉仍有效的候选；下次翻页请求
仍会重新检索和重排自己的窗口。这不是快照隔离，跨请求的窗口/页面仍可能移动。
私聊、Topics 和已撤销授权的会话遵循现有访问规则，返回的消息可以继续调用上下文接口。

## 组件及配置

1. 主应用仍使用原来的轻量镜像，不安装 PyTorch。
2. `embeddings` 是独立 CPU 服务，默认 4 个推理线程；请求排队受限，忙时返回 503。
3. `semantic-indexer` 是独立后台进程，每轮为每个 archive 处理一批消息，避免大群独占回填。
   它从各群普通消息表读数据，写入同群向量表，持续处理新增和编辑。
4. 可选 `reranker` 是**另外一个** CPU 服务，使用固定版本 `BAAI/bge-reranker-base`，
   不与向量 worker 争用服务的推理准入锁，不引入 Laya 或主应用 ML 依赖。
5. PostgreSQL 需同时安装 PGroonga 和 pgvector。此版本使用精确向量排名，成本随合格向量数增长，
   不使用可能在权限过滤后漏召回的近似索引；容量需按实际群规模及并发测量。

在主应用和后台 worker 使用的 `config.toml` 中增加：

```toml
[database.semantic]
enabled = true
endpoint = "http://embeddings:8080/embed"
batch_size = 16       # 1-32；CPU 内存紧张时减小
poll_interval = 10    # 1-3600 秒；空闲/模型故障后的轮询间隔

[database.semantic.rerank]
enabled = true
endpoint = "http://reranker:8080/rerank"
candidates = 50       # 整数 1-200；各页使用同一 offset=0 的向量候选窗口
page_size = 20        # 整数 1-50
min_score = 0.5       # 有限数值 0-1；初始阈值，需要按真实查询调优
```

**升级兼容：** 缺少 `[database.semantic.rerank]` 或其 `enabled=false` 时保持纯向量模式。
新配置示例启用重排，但全局 `[database.semantic].enabled` 仍默认 false。
启用重排不需要新的迁移、向量模型替换或索引重建。已启用但服务不可用、繁忙、超时或
返回错误模型/计数/顺序/分数时返回 503，**绝不悄悄退回纯向量结果**。
布尔、整数及数值范围在启动时验证，不接受字符串冒充布尔或数值。

重排模型 revision 固定为 `2cfc18c9415c912f9d8155881c133215df768a70`，只加载 safetensors，
禁止 remote code。查询/正文去除首尾 Unicode 空白，正文取前 8192 字符，成对输入最多
512 token，eval/inference 模式，logit 只经过一次 sigmoid。服务单次最多 32 条，HTTP 上限
2 MiB；客户端 UTF-8 JSON 分批，整个多批次调用及准入等待合计最多 60 秒。
`RERANKER_THREADS` 为 1–16（默认 4），`RERANKER_BATCH_SIZE` 为 1–32（默认 8）。
每服务同时仅一条 CPU 推理任务，取消 HTTP 请求后也要等后台线程结束才释放准入。
健康检查成功表示模型已加载，不只是 HTTP 进程已启动。

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
语义模式不改变默认轻量 linux/amd64 CI 镜像构建；可选依赖镜像在本地单独构建。
重排单独启用 `--profile rerank`，端口仅在 Docker 内部可见，不映射宿主机；
可共享 `luoxu-models` 持久缓存卷。已有语义部署只需更新应用配置/镜像并启动此服务：

```sh
docker compose -f docker-compose.yml -f docker-compose.semantic.yml \
  --profile semantic --profile rerank build reranker
docker compose -f docker-compose.yml -f docker-compose.semantic.yml \
  --profile semantic --profile rerank up -d --no-deps reranker
```

完整启用部署时，在下文命令增加 `--profile rerank`，构建列表增加 `reranker`。
只启用纯向量模式则不需要这个 profile。首次加载会下载上述固定模型，需预留 CPU 内存及时间。

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
关闭全局功能时设置 `[database.semantic].enabled=false` 并停止语义服务；
仅关闭重排则设置 `[database.semantic.rerank].enabled=false` 并停止 `reranker`。
已存向量不必删除，普通搜索照常运行。

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
  重排也不保证正确识别人名或否定关系：“说的是甲，不是乙”仍可能在搜索乙时获得高分。
  因此高分不能替代阅读原文，调高阈值也不能保证消除这类误匹配，并可能漏掉相关消息。
- 纯向量模式没有最低相似度阈值。重排初始 `min_score=0.5` 只作调参起点，
  应用实际中文查询及人工相关性判断调节：提高通常减少噪声但会漏检，降低会增加召回与噪声。
  0.8 不代表 80% 正确，更不证明事实关联。
- 重排每一页都从**向量 offset=0** 读取固定最多 `candidates` 条，而不是独立重排旧向量页。
  对整个窗口评分、过滤阈值后再按 `offset` 分页；同分按原向量排序（余弦、时间、物理键）确定。
  窗口之外的候选永远无法被此次重排找回，即使它可能有更高重排分；调大 candidates
  可扩大召回，但增加 CPU 延迟。`has_more=false` 只表示窗口内没有后续合格结果，不代表归档没有相关消息。
- 分页不是快照：编辑、删除、索引更新或授权变化可能使窗口/页面移动，结果可能减少或重复。
  请求保持查询、显式范围、过滤及 min_score 不变；没有自动补齐机制。
- 前端继续使用现有显式 `g/conversation_id`、`sender/exclude_sender`、`start/end` 过滤，
  本后端不会从自然语言推断人物、时间或跨群搜索。

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

## SQL 性能与容量检查

首轮查询在带 `LIMIT/OFFSET` 的有序子查询中计算一次原始余弦距离，外层转换为 `score`；
按原始距离、时间和物理键排序，避免按 `1-distance` 的浮点舍入结果改变顺序。
仅将**单个查询向量**物化一次，避免通用准备语句计划逐行重复执行文本到向量的转换；
不会物化整份消息/向量。重排后只回查初始键，不再做第二轮全量排名。
这不消除首次精确扫描的 I/O、内容摘要、连接和排序成本，也不代表已测得生产加速比。

- 在隔离的代表性副本上用绑定实际参数的 `EXPLAIN (ANALYZE, BUFFERS, VERBOSE)` 比较旧/新 SQL；
  `ANALYZE` 会真正执行查询。检查距离投影是否位于排序下方、分数是否仅在限量后计算，
  回查是否按三个物理键及模型使用索引（最多 200 次），而非再次顺序扫描归档。
  同时测试小/大候选窗口、过滤选择率和预备语句计划；不要只看 SQL 文本或估算 cost。
  检查通用计划应使用实际准备语句的 `EXPLAIN EXECUTE`；直接对带参数 SQL 执行 `EXPLAIN`
  可能展示定制计划，不能据此认定应用的缓存计划相同。
- 分别记录首次/冷缓存和重复/热缓存、shared read/hit、临时块读写、Hash Batches、排序落盘及并发尾延迟。
  热缓存不等于无 I/O 成本，shared hit 也不代表没有 TOAST 解压/反复取值；不要在生产清缓存做实验。
- 512 维向量可能存于外部 TOAST，表的主 heap 大小并不代表扫描总量。测量总表/TOAST 大小和缓冲读取；
  外部向量取值可能主导首次扫描。回查只用 embedding 的键、model、content_hash，
  不应解 TOAST 向量载荷（EXPLAIN 底层 heap scan 的原始 tuple 输出可能仍列出该列）。
- `work_mem` 是每个排序/哈希操作、每个 worker 的预算，并发可叠加；Hash Batches > 1 或 temp 写入
  应结合计划、容器内存限额、CPU、连接数和整体工作集评估。`shared_buffers`、OS 缓存、Docker `/dev/shm`
  各有不同用途；不要盲目提高这些值或模型线程数。本补丁不改变全局设置、索引或存储布局。
- 若单独试验向量内联存储，必须显式选择在隔离副本上比较读取成本、heap 膨胀和并发容量，再决定是否采用。
  不要只把向量设为 `STORAGE PLAIN`：受整行大小及表级 TOAST 目标影响，可能反而把 `model`、
  `content_hash` 等小字段挤到外部，增加回查/扫描成本；需测量整行及所有列，而非只看向量是否内联。
  可在副本上试验保持列存储策略不变、调整表级 `toast_tuple_target` 并受控重写；
  合成数据/内存盘结果不能作为生产 SSD 延迟承诺，也不应直接变更运行时或新表默认值。
  `ALTER COLUMN ... SET STORAGE` **不会改写已有向量**，`UPDATE embedding=embedding` 也可能保留
  原有外部指针；不能用这两步假定已完成内联迁移。实际迁移需要验证过的受控重写及事前备份、恢复演练、
  磁盘余量、WAL/复制预算、锁和停机窗口评估。不要直接在运行中的归档上批量重写。

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
