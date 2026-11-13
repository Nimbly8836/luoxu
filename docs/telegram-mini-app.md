# Telegram Mini App 登录与配置

本功能默认关闭。不需要现在提供 Bot token，也不会自动部署或修改 BotFather。

## 权限边界

Mini App 登录需要同时满足：

1. Telegram 官方 `initData` 验签成功，签发时间在有效期内，且本次启动数据未使用过。
2. Telegram 数字用户 ID 已明确绑定一个现有且启用的 Luoxu 账号 UUID。
3. 用户属于**至少一个当前正在索引的群**。

“正在索引”以数据库持久化监听引用为准：手动监听、个人授权或公开授权使 `reference_count > 0`。不使用过时的配置 `index_groups`，不包括已停用且无引用的归档或私聊；话题引用归入父群。该状态表示索引意图，不保证采集进程当时正常运行。

群成员资格只是 Mini App 准入门槛，**不会新增群授权、公开归档或授予管理员权限**。登录后沿用绑定账号现有的公开/个人授权；加入索引群 A 不会自动获得 A 的归档，也不会限制该账号原来对 B 的显式授权。非索引群成员资格不能作为准入依据。不按 Telegram 用户名匹配账号，不自动注册。

原有匿名公开访问、网页用户名密码登录不受本门槛影响；它不是全站封闭策略，也不能阻止非群成员发现或打开 Bot，只能阻止其通过 Telegram 建立/使用本站会话。

## 后端配置

先在管理界面创建所需本站账号、分配归档权限，获取其 UUID。配置示例：

```toml
[web.auth.telegram]
enabled = false # 准备好 token、迁移、Bot 群权限后才改 true
bot_token_env = "LUOXU_TELEGRAM_BOT_TOKEN"
max_age = 300
membership_cache_ttl = 60

[[web.auth.telegram.accounts]]
telegram_id = 123456789
user_id = "11111111-1111-4111-8111-111111111111"
```

- `max_age`：启动数据有效期，30–3600 秒；最多允许未来 30 秒时钟偏差。
- `membership_cache_ttl`：成员查询结果缓存，0–300 秒，默认 60。设 0 可禁用缓存，但会增加 Bot API 请求。
- `accounts` 可重复添加不同的 Telegram ID；重复 ID、非法 UUID 会在启动时报错。
- Bot token 只放服务器环境变量，不放前端配置、URL、日志或版本库。应使用承载 Mini App 的 Bot token，与 Telethon 索引账号不同。
- Compose 的 `core` / `web` 已预留 `LUOXU_TELEGRAM_BOT_TOKEN` 环境传递。默认关闭时可以不设置；开启后缺失/格式错误会阻止启动，避免误认为认证已经启用。

成员核验调用 Bot API `getChatMember`。Bot 应加入需要核验的索引群并成为管理员，Telegram 才保证能够查询其他用户。`creator`、`administrator`、`member` 允许，`restricted` 仅 `is_member=true` 时允许；`left`、`kicked` 拒绝。网络失败、超时、限流或缺少查询权限时不放行；如果能明确核验另一个索引群的成员资格，仍可通过。

Telegram 签发的 access/refresh token 保留认证来源，每个认证请求及刷新都检查当前绑定和群成员资格（允许使用有界缓存）。退出所有索引群后，最迟在成员缓存到期后的请求被拒绝；停止所有相关群的监听引用则下一次请求即不再使用这些群准入。身份绑定撤销或关闭 Telegram 登录也会拒绝已有 Telegram 会话。密码登录会话不受影响。

## 数据库升级

已有数据库先备份，再执行：

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/007_telegram_login.sql
```

新库的 `dbsetup.sql` 已包含此表。不要重新导入 `dbsetup.sql` 覆盖已有数据库。迁移只增加一次性启动数据指纹表及过期索引，不修改归档或授权；可重复执行。新功能关闭时可先不迁移，但开启前必须完成。

一次性启动防重放保存在 PostgreSQL，跨进程和重启有效。只保存验证后 MAC 的摘要及过期时间，不存原始 `initData`。成功交换后刷新页面应复用本站 refresh token。退出后重新 Telegram 登录、响应丢失或交换失败后提示“已使用”时，关闭并重新打开 Mini App 获取新的启动数据；不能复用旧启动凭据。过期记录在后续合法交换时清理。

## 前端与入口（后续部署时操作）

`Ken_luoxu_web/public/config.json` / 部署后的 `config.json`：

```json
{
  "apiBaseUrl": "/api/luoxu",
  "appTitle": "落絮",
  "telegramLoginEnabled": false
}
```

只有明确设为布尔 `true` 才会启用。普通浏览器、SDK 不可用或没有原始 `initData` 时不自动发起 Telegram 登录。Mini App 中已有本站会话优先保留；未登录时自动交换，并提供手动按钮和密码回退。

后续上线顺序：

1. 确认数据库备份和迁移，部署支持本接口的后端；保持前端开关关闭。
2. 在服务器安全配置 token、账号绑定，将 Bot 加入所需索引群并设置查询成员所需权限，再启用后端并重启对应服务。
3. 部署 HTTPS 前端并开启前端开关；反向代理转发正确 API 前缀。若有 CSP，允许官方 SDK `https://telegram.org`；只为需要的 Telegram Web 宿主配置嵌入权限，不取消 API 鉴权。
4. 在 BotFather 配置 Main Mini App 或菜单按钮 Web App URL 指向前端 HTTPS 地址。Telegram 内点击普通网页链接不等于 Mini App 启动。
5. 真实客户端验收绑定成员、非成员、未绑定成员、退出群、停止监听、token 刷新、Bot 权限不足及密码回退。

回滚时先关闭前端登录开关，再关闭后端 Telegram 登录，保留已有表即可。关闭后端后已有 Telegram 会话也会被拒绝，不影响密码会话。改账号权限仍按原有管理流程执行。

## 接口

```http
POST /api/luoxu/auth/telegram
Content-Type: application/json

{"init_data":"<SDK原始initData，仅放HTTPS请求体>"}
```

成功返回现有 `access_token`、`refresh_token`、`token_type`、`expires_in` 契约；前端接入 `configureAuthSession()` 和 `/auth/me`。401 表示启动数据无效/过期/重放、绑定无效、账号禁用或不具备索引群成员资格；503 表示功能关闭或无法确认成员资格。响应不缓存。不记录请求体或 Bot API 完整 URL；生产反向代理建议为认证接口配置适当限流。

## 本地验证

```sh
python -m unittest discover -s tests -p 'test_telegram*.py' -v
# PostgreSQL 防重放/迁移用可丢弃测试库，切勿填写生产库：
LUOXU_TEST_DATABASE_URL='postgresql://…/DISPOSABLE_TEST_DB' \
  python -m unittest discover -s tests -v
```

测试使用合成启动签名和 Bot API 响应，不需要真实 token，不访问 Telegram。真实客户端及真实群核验须留待配置入口后验收。
