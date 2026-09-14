---
status: accepted
---

# Unified conversations with explicit private-chat indexing

The archive will represent groups, forum topics, and explicitly configured one-to-one private chats as conversations. New APIs use database-generated UUID conversation identifiers, while existing group-oriented endpoints retain their Telegram group-id compatibility surface. Private chats are not indexed by default, are never available through the anonymous `pub` role, and require an explicit grant to each named user; this prevents a userbot's private dialogs from becoming accidental shared content.

Message context is read from the local archive, stays within the same forum topic when applicable, returns the configured surrounding window, and follows quoted/replied messages only to the bounded depth. Missing referenced messages are returned as unavailable placeholders instead of triggering Telegram fetches during a Web request.
