# Telegram Mini App：身份、成员交集与 Web 隔离

## 权限规则

普通 Web 和 Mini App 是不同的会话权限，不由 Telegram 用户名、前端标记或是否显示管理按钮决定。

| 入口/身份 | 内容范围 |
| --- | --- |
| 普通 Web 匿名 | 本站明确公开的群及话题，保持原规则 |
| 普通 Web 普通账号 | 公开范围加个人授权，保持原规则 |
| 普通 Web 管理员 | 全部归档，包括停用群和私聊 |
| Mini App 主人 | **本人当前加入 ∩ 本站当前索引**的群，包括未公开群 |
| Mini App 其他用户 | **本人当前加入 ∩ 本站当前索引 ∩ 本站明确公开**的群 |
| Mini App 没有符合条件的群/身份无法核验 | 前端静默空白，不回退到匿名网页，不显示群名、数量或拒绝原因 |

“主人”只能通过服务器配置 `owner_telegram_id` 指定，建议设为索引账号本人的数字 ID。不是任何管理员都算主人，也不根据用户名猜测。没有配置主人时，所有 Telegram 用户都走公开群成员交集。

“当前索引”指持久化监听引用大于零（手动、个人授权或公开授权），不是采集进程的实时成功保证。已停用且没有引用的旧归档不在 Mini App 范围内。普通访客要求**父群显式公开**；仅公开一个话题不会把整群纳入候选。话题继承已经核验通过的父群范围。私聊不进入 Mini App。管理员全归档能力仅保留在 Web 会话。

公开范围仍是整个 Web 的公共信息。别人直接访问普通网址/匿名 API 仍能看到公开群，这一点不因 Mini App 静默而改变，也无法靠浏览器环境标记阻止。Mini App 隔离旨在不主动向未核验的 Telegram 用户展示信息，不宣称隐藏公网已有数据。

## 后端配置

```toml
[web.auth.telegram]
enabled = false # 完成007/008迁移及配置后再启用
bot_token_env = "LUOXU_TELEGRAM_BOT_TOKEN"
owner_telegram_id = 123456789 # 索引账号本人的数字ID，绝不是Bot ID
max_age = 300
membership_source = "auto"
membership_cache_ttl = 60

# 可选：保留主人与现有Web账号的关联，不扩大Mini App权限。
[[web.auth.telegram.accounts]]
telegram_id = 123456789
user_id = "11111111-1111-4111-8111-111111111111"
```

- `max_age`：新启动数据有效期，30–3600秒，默认300；允许未来30秒时钟偏差。
- `membership_cache_ttl`：成员核验结果缓存0–300秒，默认60。退群后的下一次请求最迟在缓存过期后拒绝；设0增加Telegram请求量。
- `accounts`：可选现有账号绑定。绑定账号必须启用。**即使绑定Web管理员，Mini App也不是管理员，不允许管理接口，不继承个人授权或全归档能力。**
- 其他符合条件的Telegram用户无需预建账号：仅在验签及至少一个可见群核验通过后，创建稳定的Telegram专用本站身份。随机内部用户名，不按Telegram用户名匹配；无可用密码、无管理员角色、无群授权，不增加监听引用。账号映射存储在 `auth_telegram_accounts`。管理员可禁用该身份，已有会话随即失效。
- token只在服务器环境变量中保存。Compose预留 `LUOXU_TELEGRAM_BOT_TOKEN`，绝不提供给前端、URL或日志。启用时缺token或缺迁移直接启动失败。

## Userbot 与 Bot API

| `membership_source` | 行为 |
| --- | --- |
| `auto`（默认） | 每群优先使用索引器现有Telethon用户客户端，无法确认时回退Bot API |
| `userbot` | 仅复用索引器用户客户端；需要core，不新建/重登session |
| `bot` | 仅Bot API `getChatMember`，支持独立Web服务 |

Userbot方式不要求Mini App Bot入群。超级群/频道使用 `channels.getParticipant`，普通群使用 `messages.getFullChat` 成员元数据。索引账号本人也必须确认成员资格，没有免检。未知access hash、隐藏成员、离线、权限不足、限流视为无法确认，不放行。Bot方式需Bot能查询对应群成员，通常需要加入并设为管理员。

确认仍在群的管理员、普通成员及禁言成员允许；已退出或踢出的用户拒绝。`auto`明确查到非成员时不会再用Bot结果覆盖拒绝。查询有单次和总时限；**只返回实际已确认的群**，其他群失败不能扩大范围；没有任何确认通过的群则拒绝。不是“任意一群通过就取得全部权限”。即使只用Userbot，Bot token仍用于Mini App启动数据验签。

每个Telegram认证请求及refresh重新查询候选范围，并按缓存策略复核每群成员资格。发布状态、监听引用在内容权限解析时再次检查，避免慢搜索结束后使用已撤销公开范围。群列表、搜索、上下文、历史、删除快照和头像共用受限范围；管理接口始终拒绝Mini App会话。原有历史/语义功能开关仍有效。

## 前端静默与会话隔离

部署 `config.json`：

```json
{
  "apiBaseUrl": "/api/luoxu",
  "appTitle": "落絮",
  "telegramLoginEnabled": true,
  "defaultGroupId": ""
}
```

检测到Mini App启动后，先加载SDK和配置，再将原始 `initData` 交给后端验证。只有受限Telegram身份和实时群列表确认成功后才渲染业务界面。SDK失败、空启动数据、开关关闭、登录拒绝或群列表失败都保持空白；不弹密码登录、不匿名拉取群列表、不响应未认证hash搜索。

不复用 `luoxu_access_token`、`luoxu_refresh_token`、`luoxu_user` 等普通Web本地会话。Mini App access token仅在内存，refresh token只保存在独立sessionStorage键 `luoxu_miniapp_refresh_token`。不持久化原始initData。重新加载仍先提交当前SDK签名数据；后端只允许与该Telegram ID相符的有效受限refresh token恢复，不能拿上一个Telegram账号或Web管理员的缓存会话进入。

旧群名缓存不会预填界面，必须等待实时受限列表；默认群/启动参数也只能从该列表选取。会话失效时清除当前业务状态并静默，不循环匿名重试。Web账号存储不受影响。

## 接口与防重放

```http
POST /api/luoxu/auth/telegram
Content-Type: application/json

{"init_data":"<SDK原始签名数据>","refresh_token":"<可选的Mini App专用刷新令牌>"}
```

- 没有匹配的有效refresh token时，按HMAC、时间有效期和一次性指纹验签/交换。已使用的启动数据不能再次创建会话。
- 有效refresh token必须属于签名数据中的同一Telegram ID、同一当前账号及新版受限会话。此时可接受该页面旧启动数据，但仍验签并轮换refresh token、核验当前成员范围。不匹配的token不能恢复旧启动数据。
- 成功返回现有 `access_token`、`refresh_token`、`token_type`、`expires_in`。
- `/auth/me` 为Mini App返回 `auth_source="telegram"`、`telegram_scope_version=1`、Telegram ID及 `is_admin=false`。角色和群权限仍由服务器独立检查，前端字段不是授权来源。
- 旧Telegram token没有范围版本会被拒绝；旧Web token在普通Web仍有效。刷新和每次请求不会丢掉认证来源。
- 无身份或无可见群401；无法核验503。前端两者均静默，不宣称网络失败等同非成员。
- 认证/个人响应不缓存。反向代理建议限制认证请求速率，并禁止记录请求体及含token的Bot API URL。

## 私聊命令设置个人默认群

可选开启 `[web.auth.telegram.commands].enabled=true` 后，Bot接收器会注册并处理私聊 `/default`：选择、分页、直接指定群ID或 `/default clear`。默认值按Telegram用户保存，Mini App下次打开时读取，优先于站点默认但不覆盖明确hash/start参数。列出和设置均沿用成员交集，其他人无可见群时保持静默；不更改Mini App入口或创建群授权。

需新增 `009_telegram_default_groups.sql` 迁移，并保证没有其他Bot轮询器/webhook。现有webhook不会被自动删除；菜单注册范围为all_private_chats。完整操作、限制和验收见[个人默认群命令](telegram-default-command.md)。

## 升级与回滚

已有数据库先备份，执行尚未应用的迁移：

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/007_telegram_login.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/008_telegram_accounts.sql
```

新建库 `dbsetup.sql` 已包含上述表及可选命令使用的偏好/游标表。007保存一次性启动摘要和过期时间；008保存Telegram数字ID与本站账号UUID映射。均不修改归档、公开范围或个人授权，不重导dbsetup覆盖现有库。008可重复执行。启用个人默认命令前还需执行 `migrations/009_telegram_default_groups.sql`；命令默认关闭时不要求009。

上线建议：先部署严格静默的新前端（旧后端缺受限会话标记时它会保持空白），再备份/迁移、配置 `owner_telegram_id` 并更新后端；或在维护窗口协调切换。随后关闭并重新打开已有Mini App页面，验证主人/不同成员/非成员。旧前端仍可能回退匿名页面，不能把只更新后端或仍打开的旧页面当作已完成隐私升级。未配置owner时主人也只有公开成员交集。

入口在BotFather配置Main Mini App或菜单按钮HTTPS URL。普通网页链接不等于Mini App启动。CSP按需允许 `https://telegram.org` SDK及需要的嵌入宿主，不取消鉴权。

回滚到旧版本会恢复旧Mini App权限及匿名回退行为，因此**不能在继续宣称新隐私规则有效的情况下回退**。需停止Mini App入口/维护，再回退或修复；仅关闭后端登录在旧前端上可能触发匿名展示。保留新增表及身份，不倒灌归档。真实客户端验收不是合成测试的替代。

## 测试

```sh
python -m unittest discover -s tests -p 'test_telegram*.py' -v
LUOXU_TEST_DATABASE_URL='postgresql://…/DISPOSABLE_TEST_DB' \
  python -m unittest discover -s tests -v
```

测试只使用可丢弃数据库、合成签名和模拟Telegram成员接口，不触碰生产数据。范围回归包括主人/访客/Web身份隔离、每群交集、私聊排除、话题继承、撤销公开、退群、停止监听、旧token拒绝、身份创建并发和刷新绑定当前签名身份。
