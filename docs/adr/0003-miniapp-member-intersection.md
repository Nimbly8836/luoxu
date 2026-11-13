---
status: accepted
---

# Separate Mini App membership scope from ordinary Web access

The operator accepts that anyone knowing the ordinary Web URL can access public
archives, but a discoverable Telegram Bot must not reveal the owner's group list
to unrelated Telegram users. Other members may use public indexed groups they
personally belong to. This narrows ADR 0002 for Telegram sessions only.

## Decision

The configured owner sees joined, actively indexed groups. Other Telegram users
see the intersection of joined, actively indexed, explicitly public parent groups.
Topics inherit only verified parents. Private chats and management APIs are not
available through Telegram sessions, even when linked to a Web administrator.
Current publication/monitoring is rechecked in the common content scope resolver;
Telegram membership uses a bounded cache. Unknown membership never grants access.

Eligible unbound visitors receive stable Telegram-only local identities without
passwords, admin privileges or grants. This does not create indexing references.
New migration 008 stores these identities; existing explicit bindings remain
optional. Web authentication and public/admin permissions remain unchanged.

The frontend stays blank until current signed Telegram identity and the live
scoped group list are confirmed. No Web-session reuse, anonymous fallback,
password panel, pre-auth group snapshot, or pre-auth hash search is permitted.
Resume tokens must match the current signed Telegram user at the server; scopes
are recomputed rather than frozen into a token. Old unscoped Telegram tokens fail
closed. Shared-server anonymous APIs remain public by design, not a secret API.

## Rollout boundary

Backend and frontend must be updated together after migration/configuration.
Old frontends may show public content after auth denial; rolling back to them
cannot retain the new silent behavior. Actual Telegram client verification is
required after the synthetic membership and isolated browser/DB regressions.
