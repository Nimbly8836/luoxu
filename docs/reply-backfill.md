# 旧归档回复关系补录（离线维护工具）

旧版上游 Luoxu 可能已保存消息正文，却没有 `reply_to_id`。本工具只为**已有群会话中的已有消息**补上缺失的同 peer 直接父消息 ID，不重新导入历史消息。

这里的“离线”指**停止采集后的维护窗口**，不是断网运行：预览和执行都需要连接 Telegram 和 PostgreSQL。交付工具不表示已对生产运行补录，也不保证 Telegram 全部讨论串完整。

## 安全边界

- 目标必须是 `conversations.id` 的 UUID，且 `kind=group`、peer 类型为 `chat` 或 `channel`；不是 Telegram 群数字 ID。一次只处理这个精确会话，不遍历话题、私聊或其他群。所有消息查询路由到该 peer 的独立普通表，不使用全局消息表或年度分区；写入与采集、删除和话题修复共享 peer 事务锁。
- 只请求本地已有、最新存储版本未删除且 `reply_to_id IS NULL` 的消息 ID；按 ID 升序分页，每批 1–100 条。不抓取父消息，不下载媒体，不运行 OCR。
- Telegram `get_messages` 返回完整 Message 对象，但工具只将其用于 peer、消息 ID、日期和回复头校验；不保存或打印返回的正文。不会根据正文猜测父子关系。
- 写入前，数据库再次校验完整 peer（类型和 ID）、有效正整数且非自身的直接本地回复 ID、Telegram 日期与最新存储版本的精确日期一致，以及删除/非空保护。已有关系不覆盖，删除内容不恢复。
- 不改正文、编辑状态、删除状态、OCR、`tg_groups` 历史游标、授权、监听引用；不创建会话、消息或修订记录。无需新 schema migration，工具也不会执行迁移、bootstrap 或自动停服务。
- `None`、`MessageEmpty` 或缺失返回项表示“无法取得”，**不是确认无父消息**。返回其他 peer、重复 ID、未请求 ID 或非法对象时，整批拒绝写入。
- 权限、认证、RPC 错误与超时会失败退出，不当作空结果或推进失败批次的断点。账号必须已有有效登录 session；工具仅 `connect` 和检查授权，不调用交互登录或 `start`。

## 执行前

1. 确认运行版本包含 `luoxu.backfill_replies` 和对应数据库接口；不要假定某个已发布镜像已经包含本功能。已有数据库必须先完成 `006_per_group_storage.sql`，见[独立群表存储](group-storage.md)；工具不会自动迁移。
2. 按现有运维流程备份 PostgreSQL，保留配置、Telegram session 和既有 Docker volumes；安排维护窗口。
3. **如果使用相同 `telegram.session_db`，必须先停止 core 以及其他使用该 session 的客户端。预览也必须遵守。** 工具只警告，不检测或自动停止它们。
4. 独立 Python Web 可以继续提供读取服务。不要同时做数据库恢复、群/话题归档搬迁或其他维护。不同断点文件的并发任务不会被同一文件锁拦住，因此不要用多个维护进程共享同一 session。
5. 从现有管理列表确认会话 UUID、peer 类型与 ID、Telegram 登录账号和目标数据库。工具使用配置中的 `database.url` / `first_year`，明确排除 OCR 配置。
6. 断点放在**持久、仅运维用户可写的本地目录**，目录须提前存在。Linux/macOS 的 `fcntl` 文件锁是必需的；不支持的平台明确报错。不要依赖不保证 `flock`/原子替换的网络文件系统。

## 命令与范围

```sh
python -m luoxu.backfill_replies \
  --config /path/to/config.toml \
  --conversation-id "$CONVERSATION_UUID" \
  --state-file /path/to/persistent/replies.json
```

默认是 **dry-run，仅预览下一批**；不会更改数据库、创建或更新 JSON 断点。为防止同一断点并发使用，仍会创建/保留同名 `.lock` 文件。预览输出的 `last_id` 是本次预览扫到的位置，不是已写入的断点。

| 参数 | 默认值与含义 |
| --- | --- |
| `--apply` | 不指定则只预览；指定后处理整个固定范围，除非达到 `--max-batches` |
| `--batch-size` | `100`，整数 1–100 |
| `--delay` | `1` 秒，两批之间等待；必须有限、`0 < delay <= 3600`，不允许 0、NaN 或无穷 |
| `--after-id` | `0`，**不含**此 ID；恢复时须保持最初的值，不填当前断点 |
| `--through-id` | **含**此 ID；新任务不指定时取目标归档当前最大消息 ID，空归档为 0 |
| `--max-batches` | 默认不限批数；指定时必须为正整数，可先只执行 1 批 |
| `--max-flood-wait` | `300` 秒，允许范围 0–86400；0 表示不等待 FloodWait |

消息 ID 范围为 0–2147483647，`after_id <= through_id`。每次 Telegram RPC 最长等待 60 秒。FloodWait 超过配置上限直接失败；在上限内最多重试 3 次（每次 RPC 最多 4 次尝试），零秒 FloodWait 也至少等待 1 秒，否则失败。普通 RPC 错误不自动重试；不会无限循环重试。

新 apply 任务先保存绑定的范围和初始游标，再开始批次；因此第一批出错也可以保留固定上界。每批数据库事务提交返回后，才保存最后一个**请求过的**消息 ID。原子保存使用 0600 临时文件、flush/fsync、`os.replace` 和目录 fsync。

## Docker：预览、执行一批、恢复

以下为示例，不含真实 UUID 或凭据。**所有命令必须保留现有 `-f` 覆盖文件、顺序、Compose 项目名、环境文件以及 volumes。** 如果平时使用 `--env-file`、`-p` 或其他覆盖文件，也把它们加入下面的函数；没有覆盖文件的部署不要照搬不存在的文件。不要另起新 Compose 项目，也不要 `down -v`。

```sh
# 改为自己部署原本使用的完整 Compose 参数；不要丢掉 -f 覆盖配置。
dc() {
  docker compose -f docker-compose.yml -f /path/to/existing-override.yml "$@"
}

# 使用已确认含此工具的版本，并保持已有配置及 volumes。
CONVERSATION_UUID='<归档群会话 UUID>'
STATE='/data/reply-backfill-case-01.json'

# 只停止采集；已有 db / 独立 web 保持运行。外部数据库沿用原覆盖配置。
dc --profile core stop core

# 预览一批（仍会连接 Telegram）。/data 来自原 core 的持久 session volume。
dc --profile core run --rm --no-deps -T core \
  python -m luoxu.backfill_replies \
  --config /config/config.toml --conversation-id "$CONVERSATION_UUID" \
  --state-file "$STATE" --batch-size 100 --delay 1

# 经人工核对后，先提交一批。
dc --profile core run --rm --no-deps -T core \
  python -m luoxu.backfill_replies \
  --config /config/config.toml --conversation-id "$CONVERSATION_UUID" \
  --state-file "$STATE" --batch-size 100 --delay 1 --apply --max-batches 1

# 恢复并扫完已固定的范围：同一个 JSON、同一个 UUID、同一个账号和初始范围。
dc --profile core run --rm --no-deps -T core \
  python -m luoxu.backfill_replies \
  --config /config/config.toml --conversation-id "$CONVERSATION_UUID" \
  --state-file "$STATE" --batch-size 100 --delay 1 --apply

# 完成核验、确认所有维护进程退出后，按原部署方式启动 core。
dc --profile core up -d --no-build --no-deps core
```

`run --no-deps` 不启动数据库、OCR 或其他依赖，也不发布 core 服务端口；依赖必须已可用。它复用 core 的配置/session/cache 挂载。不要将断点写到 `--rm` 容器的临时可写层，不要挂载新的空 volume 替代现有 session。以上示例不会自动运行 schema migration。

若首次使用 `--after-id 100 --through-id 1000`，恢复时必须继续传 `--after-id 100`；`--through-id 1000` 可保留或省略，省略时复用 JSON 内的 1000。不要将 `--after-id` 改为已处理游标；修改范围须换新断点文件。

## 断点、失败与再次补录

JSON v1 绑定规范 UUID、peer 类型和 ID、Telegram 账号 ID、初始 `after_id`、固定 `through_id`、`last_id` 和 `complete`。恢复会拒绝作用域/范围/账号不匹配、损坏 JSON、重复字段、未知字段或不一致游标，不会覆盖错误状态。

- 同一断点的 `.lock` 文件以非阻塞 OS 锁保护全程；第二个任务立即失败。进程结束会释放锁，但**不要删除 `.lock` 文件**，它的稳定 inode 用于防并发。
- 提交成功后、断点写入前崩溃时，数据库可能已有补录而 JSON 仍旧；恢复仍安全，已有非空关系不会重写。失败仅意味着本次未完整完成，不代表之前所有提交已回滚。
- 不可用消息、没有有效直接回复头的消息、日期校验不通过的消息都可能仍保留空关系，但已经扫过的 ID 会前进，以免无限重试。要再次尝试它们，使用**新的断点文件**和原范围；已有补录会被自动排除。
- 原任务的上界不会因新归档增长而扩大。要处理新增范围，使用新断点文件并明确范围；不要改写原 JSON。
- **数据库恢复备份后，弃用该数据库恢复前的断点，重新创建断点从需要的范围扫描。** 同 UUID/peer 的断点无法识别数据库被恢复，旧游标可能跳过已回滚的数据。
- Ctrl-C 返回 130，其他运行错误返回非零；限批正常退出仍可能 `complete=false`。日志只输出批次计数/ID，不输出正文、凭据、session keys 或原始配置。不要通过降低日志保护来公开敏感配置排障。

## 核验结果

标准输出是本次调用的 JSON 汇总；进度日志写 stderr：

- `scanned`：本次请求的候选消息 ID 数。
- `filled`：apply 实际填写的物理行数；dry-run 为符合填写条件的行数。
- `unavailable`：本批缺失/`None`/`MessageEmpty` 的请求 ID 数，不包含已取得但无有效父关系的消息。
- `last_id`、`through_id`：本次扫描位置和固定上界。
- `complete`：该固定本地候选范围已扫完；**不是 Telegram 历史或完整讨论串已全部取得的证明**。
- `dry_run`：是否仅预览。统计仅涵盖本次调用，不累计前次运行。

通过现有消息详情或 context API 检查已知样本的父关系和线程，确认正文、编辑/删除状态保持不变。父 ID 指向的原文若未归档或无访问权限，仍应显示不可用占位；工具不会补抓原文、解除授权或恢复已删除正文。

自动回归使用现有 disposable PostgreSQL/PGroonga 库并按 schema 隔离；只 mock Telegram 传输与等待，不访问真实 Telegram：

```sh
# LUOXU_TEST_DATABASE_URL 应由测试环境提供，绝不能指向生产。
python -m unittest discover -s tests -p test_reply_backfill.py -v
```

参见 [Docker 部署](docker.md)、[消息上下文](message-context.md) 和 [群访问与监听](group-access.md)。
