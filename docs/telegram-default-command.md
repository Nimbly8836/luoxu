# 私聊 `/default`：个人默认群组

这是完整的Bot命令接收、按Telegram用户持久化和Mini App读取流程，不只是菜单描述或一次启动链接。默认关闭，需迁移和配置后启用。

## 使用

在Bot私聊里发送：

- `/default`：显示自己当前可见的群，点击按钮设置个人默认群。每页8个，支持翻页和清除。
- `/default <群ID>`：直接设置当前可见群；接受本站正数群ID和Telegram `-100…` 等标记ID，不按名字猜测。
- `/default clear`：清除个人默认群，恢复站点默认值/唯一可见群逻辑。

设置后**关闭并重新打开Mini App**。优先级为：明确的 `#g=` → 启动 `startapp` 参数 → 个人默认群 → 站点 `defaultGroupId` → 唯一可见群。因此如果BotFather的固定URL带了 `#g=…`，个人默认不会覆盖它；想用个人默认时应移除固定群参数。

按钮和消息处理窗口为5分钟，过期请重新发送 `/default`。初次启用时以前积压的旧命令不会突然更改偏好或发送旧群列表。

## 权限与静默

- 只处理私人聊天，且聊天ID必须等于经过Telegram Bot API确认的发送者ID。不向群聊回复群列表；转发命令也忽略。
- 每次列出、翻页或设置都重新走Mini App成员交集：主人只看已加入且索引的群；其他人还要求父群公开。成员缓存沿用 `membership_cache_ttl`，不新建额外授权。
- 现有绑定/自动身份被禁用时不处理；无可见群、成员核验失败、伪造或不可访问的群选择均静默。不确认猜测的群是否存在。
- Callback仅空白确认以停止客户端加载状态，不显示拒绝原因。按钮数据不是权限凭据，旧按钮会重新检查公开/监听状态。
- 按Telegram数字ID保存，不按本站账号UUID或用户名共享。因此即使有账号绑定，也不会串用别人的默认值。
- 偏好仅存 `telegram_default_groups`，不创建账号、群授权、公开授权或监听引用。只在实际Mini App登录时才按原规则创建符合条件的Telegram身份。
- `/auth/me` 在该功能开启时返回Telegram会话的 `default_group_id`，值为本站群ID字符串；只有默认群仍在本次已确认的权限范围内才返回，否则为 `null`。Web会话不返回这个偏好。保存旧UUID不会扩大权限，私聊和话题不能直接设为默认父群。

## 开启

已有Telegram登录的部署只需新增009迁移，不必重新导入数据库：

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/009_telegram_default_groups.sql
```

全新库已包含该表。009可重复执行，仅新增偏好和Bot更新游标表，不动归档/授权。

```toml
[web.auth.telegram.commands]
enabled = true
```

同时要求 `[web.auth.telegram].enabled=true`，沿用现有Bot token、主人ID和成员核验方式。启用时缺009表会拒绝启动。此开关控制命令接收和个人默认值读取；关闭后记录仍保留，但前端恢复站点默认逻辑。

更新支持该功能的前后端，并重启对应后端服务后，后台接收器会：

1. 检查Bot是否已有webhook；**已有则暂停命令轮询、定期复查，不删除或覆盖webhook**。
2. 在 `all_private_chats` 范围注册 `/default`，保留已有的其他命令。命令描述不包含任何群名。
3. 通过 `getUpdates` 长轮询接收命令/按钮，不需要新增公网webhook或端口。

不更改Main Mini App URL或菜单按钮。如果菜单按钮已配置为打开Web App，仍可在私聊输入框键入 `/` 查看命令，或直接输入 `/default`。Telegram客户端可能缓存菜单，需要重新打开聊天。排查菜单时应查看 `getMyCommands` 的 **all_private_chats** 范围，不要仅看默认范围。

## 单实例、恢复与限制

- 每个Bot只允许一个更新接收器；不要与其他 `getUpdates` 程序或webhook同时启用。
- 同数据库的实例使用按Bot ID区分的PostgreSQL advisory lock，另一个实例等待，不重复注册/消费。接收器占用一个数据库连接；应用正常连接池应另有连接可供权限查询使用。
- 更新游标保存在 `telegram_bot_offsets`。成功处理/明确忽略的更新才推进游标；临时API错误重试。偏好写入幂等，但网络丢失响应时可能重复发送确认消息，不承诺外部消息exactly-once。
- 被用户屏蔽、过期callback等400/403不会永久堵塞队列；401表示授权失败，停止轮询，需检查token配置。
- 409冲突不再永久结束任务：按60、120、240、300秒（上限）冷却重试，只有成功完成一次轮询才重置退避。冷却期间保留数据库锁，避免其他副本立即接手争抢；重新尝试前再次检查webhook。已有webhook时仅定期复查，不轮询、不删除或替换它。
- 冲突可能来自另一个消费者，也可能是网络断开后尚未结束的旧请求；持续出现时仍需排查其他消费者，而不是依赖自动重试抢占。日志仅记录固定状态、状态码和重试秒数，不打印token URL、消息正文或群名。
- 有索引客户端时等它连接后再轮询；独立Web可用Bot API核验，但仍受原有Bot查群权限限制。
- 关闭应用会取消轮询并释放锁。关闭功能不会自动删除远端命令菜单，避免干扰其他接管实例；永久停用时可只移除 `/default` 项，勿清空其他命令或修改Mini App入口。
- Bot接收器故障不应拖垮现有网页/索引服务。应检查日志中的 `Telegram default commands ready`、`conflict`、`webhook`、`blocked` 或 `retrying`，不能只凭网页200判断命令已正常运行。

## 测试与上线顺序

```sh
python -m unittest discover -s tests -p test_telegram_commands.py -v
LUOXU_TEST_DATABASE_URL='postgresql://…/DISPOSABLE_TEST_DB' \
  python -m unittest discover -s tests -p test_telegram_defaults.py -v
```

仅使用合成Bot API和可丢弃数据库，覆盖私聊/权限隔离、选择/清除/分页、失效默认不返回、迁移、菜单合并、webhook冲突、轮询互斥与重启游标。前端另有默认优先级与浏览器回归。

建议先备份/迁移并更新前端，再更新后端及启用命令；不要先注册一个无处理器的菜单。上线后重新发送 `/default`，分别用主人、公开群成员和无可见群用户验证。回滚时保留009表与偏好，继续遵守Mini App严格静默的回滚边界。
