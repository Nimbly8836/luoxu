功能说明
====

落絮可以 CJK 友善的方式索引 Telegram 群组与频道，并提供 API 供前端使用。

风险提示
====

本程序使用 userbot，有被封号的风险。请三思而后行！

安装与配置
====

配置 luoxu
----

* 安装 Rust nightly 以及 [OpenCC](https://github.com/BYVoid/OpenCC) 库。

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
rustup toolchain install nightly
```

* Python 库依赖请见 `requirements.txt` 文件
* [获取](https://core.telegram.org/api/obtaining_api_id)一份 Telegram API key
* 在 `querytrans` 目录下运行 `rustup run nightly cargo build --release` 然后把生成的文件（`target/release/libquerytrans.so`）复制为 `querytrans.so` 并放在 Python 能找到的地方（比如当前目录）需要注意的是：构建 `querytrans` 之前需要提前准备好 python 环境，如果与运行 luoxu 的环境不一致将会出错。
* 复制 `config.toml.example` 并按需要修改
* （可选）词云插件需要在 `luoxu-cutwords` 下运行 `cargo build --release` 并将生成的可执行文件放到 `$PATH` 中

使用 `python -m luoxu.ls_dialogs` 可以列出会话的 id 和名称。频道和群组的 id 可以用于配置文件中。

设置数据库
----

* 安装 PostgreSQL 及 pgroonga
* 使用 `createdb` 命令创建数据库
* 使用 `postgres` 用户身份连接到该数据库，并执行 `CREATE EXTENSION pgroonga;`
* 导入 `dbsetup.sql` 脚本，如 `psql DBNAME < dbsetup.sql`

运行
----

在本项目目录下（或者将本项目目录加入 Python 模块路径），执行：

```sh
python -m luoxu
```

配置 Web 前端
----

* 可使用 [luoxu-web](https://github.com/lilydjwg/luoxu-web)
* 修改 `src/App.svelte` 中的 API URL
* 运行 `npm run build` 编译文件
* 使用 Web 服务器 serve `dist` 目录

你也可以使用 Vercel 来方便快捷地部署此项目。

当然了，你也可以自己另外编写 Web 页面来使用此 API。

注意事项
====

不要直接将索引了私有群组/频道的 API 公开于网络上！任何人都能获取其内容的。公开索引了公开群组或频道的服务前，也请获取群组/频道管理员的同意。

luoxu 相当于运行一个 Telegram 客户端，其权限是完全的（包括创建新的登录、结束已有会话、设置两步验证等）。请注意保护账号安全，不要在不信任的服务器上登录与运行本项目！

请注意配置数据库权限。Arch Linux 上默认安装的 PostgreSQL 很可能是对本地用户不设防的（请查阅 `/var/lib/postgres/data/pg_hba.conf` 配置文件）。

使用
====

搜索消息时，搜索字符串不区分简繁（会使用 OpenCC 自动转换），也不进行分词（请手动将可能不连在一起的词语以空格分开）。

搜索字符串支持以下功能：

* 以空格分开的多个搜索词是「与」的关系
* 使用 `OR`（全大写）来表达「或」条件
* 使用 `-` 来表达排除，如 `落絮 - 测试`
* 使用小括号来分组

数据库升级
====

当对 PostgreSQL 进行跨版本升级时，需要额外处理 pgroonga 的事情。

步骤示意：

1. 安装新的数据库软件
2. `cp /usr/lib/postgresql/pgroonga* /opt/pgsql-13/lib`
3. 安装新的 pgroonga
4. 执行升级（`pg_upgrade`）
5. 如果 pgroonga 版本已更新，执行 [pgroonga 升级流程](https://pgroonga.github.io/upgrade/)
6. 升级完成之后需要重新索引（如果索引已被连带删除，从 SQL 文件中找到创建索引的语句并执行）：

```sql
reindex index usernames_idx;
reindex index message_idx;
```

不兼容的变更
====

* 2025年06月29日, 更新了 OCR 服务的响应格式。请配合新版 [paddleocr-web](https://github.com/lilydjwg/paddleocr-web/commit/8d08d1332ef8df9aa25a256456a5986445005c75) 使用。

认证、权限与 API
====

Web API 支持账号密码登录和 JWT Bearer Token。未认证请求使用匿名 `pub` 角色，只能访问数据库中明确公开的群；登录用户继承公开群权限，并叠加管理员授予的群或私聊会话权限。私聊必须在 `telegram.index_private_chats` 中明确配置，且不会对匿名用户公开。

在 `[web.auth]` 中配置随机 `jwt_secret`、token 有效期以及一次性的 bootstrap 管理员。管理员密码必须使用 Argon2id 哈希，不能写明文。管理员通过 `/admin/*` API 管理用户和会话授权。完整接口规范位于 `openapi.yaml`，运行后可在 `/luoxu/docs` 查看 Swagger UI。

消息上下文使用 `/conversations/{conversation_id}/messages/{message_id}/context`，默认返回前后各 5 条消息以及最多 5 层回复链。消息编辑/删除历史由 `[web.message_history].enabled` 控制，默认关闭；开启后还必须在请求中传 `include_history=true`。历史只从功能启用并在线捕获之后开始记录。

数据库升级
====

全新数据库执行 `dbsetup.sql`。已有数据库按顺序执行 `migrations/001_access_control.sql`、`migrations/002_conversations.sql` 和 `migrations/003_message_history.sql`。升级前请备份数据库。

Docker
====

仓库提供主功能镜像、PaddleOCR 镜像和带 pgroonga 的 PostgreSQL Compose 服务。主功能镜像会由 GitHub Actions 自动构建并发布到 GHCR：

```sh
docker pull ghcr.io/nimbly8836/luoxu:latest

# 默认使用 GHCR 镜像；也可以传 --build 从当前源码本地构建
docker compose --profile core up -d
docker compose --profile core --profile ocr up -d
# NVIDIA GPU OCR（需要 NVIDIA Container Toolkit）
docker compose -f docker-compose.yml -f docker-compose.gpu.yml \
  --profile core --profile ocr up -d
```

复制 `config.toml.example` 为 `config.toml` 后再启动 Compose。数据库使用 named volume 持久化，OCR 默认 CPU 运行。Compose 中可通过 `LUOXU_IMAGE` 覆盖主应用镜像，例如 `LUOXU_IMAGE=ghcr.io/nimbly8836/luoxu:sha-...`。

* [2022年06月23日](update-2022-06-23.md)，采用分区表来提升部分查询的性能。需要更新配置文件及数据库。
