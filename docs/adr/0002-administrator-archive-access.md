---
status: accepted
---

# Administrators implicitly read all archived conversations

The operator requires the default administrator to see every archive without
per-group grants. This updates the administrator read policy in ADR 0001;
anonymous and ordinary-account authorization remains unchanged.

## Decision

An authenticated, active account whose current database `is_admin` is true can
read every archived conversation: groups, topics and private chats, including
stopped archives and conversations created later. The shared accessible-ID
resolver applies this consistently to lists, keyword/semantic search, message
content, context, captured history, deleted snapshots and sender avatars.

The administrator username and stale JWT role claims do not grant this access.
The HTTP middleware reloads the account on each authenticated request, so
demotion and disabling affect old tokens immediately. Telegram signature,
account binding and Mini App membership checks still apply independently.

## Consequences

No grants are inserted, no archives are made public, and no monitoring references
are created. Implicit visibility cannot keep an otherwise stopped group indexing.
Ordinary users still need public or individual grants; private chats cannot be
made public. Existing search-scope requirements and feature switches still apply:
admin visibility does not enable global search, capture missing history, or turn
on disabled semantic/history functionality. No database migration is required.
