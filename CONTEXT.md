# Luoxu message archive

This context defines the people, access boundaries, and Telegram content concepts used by the Luoxu archive.

## Access

**Anonymous visitor**:
A request that has not established a user identity. An anonymous visitor is evaluated through the `pub` access role and never represents a login account.

**Authenticated user**:
A person with a Luoxu login identity. Their access includes groups available to the `pub` role plus groups explicitly granted to that identity; private conversations still require an explicit grant.

**Access role**:
A named permission boundary used to decide which indexed groups an identity may see and search. `pub` is the access role for anonymous visitors.

**Accessible group**:
An indexed Telegram group that the current identity is allowed to list, search, and inspect through the archive. The anonymous `pub` role can only receive explicitly granted public-group access.

**Monitored group**:
A Telegram group selected for message indexing. Starting monitoring does not make its archive public or grant any account access.

**Public group archive**:
An archive explicitly accessible to the anonymous `pub` role and therefore to every authenticated user. This is independent of whether the Telegram group has a public username. A group requiring per-account visibility must not receive public access.

**Administrator**:
An account allowed to enumerate all archived conversations, start monitoring, and manage accounts and grants through administration APIs. Administrative management authority does not bypass grants in ordinary content APIs; administrators can explicitly grant their own accounts access.

**Private conversation**:
An indexed one-to-one Telegram conversation. It is not an accessible group and is denied to anonymous visitors by definition; a named user needs an explicit grant for the conversation.

## Content

**Conversation**:
A searchable Telegram exchange represented in the archive. A conversation may be a group, a forum topic, or an explicitly indexed one-to-one private chat; each has its own access grants.

**Forum topic**:
A named discussion area in a Telegram group with Topics enabled. Ordinary message replies and their reply threads are not forum topics and remain part of the group conversation.

**Message context**:
The nearby conversation needed to understand a selected message, including its quoted or replied-to chain up to the supported depth. The surrounding window stays within the same group topic when the group uses Topics; unavailable referenced messages are represented as placeholders.

**Message change record**:
A historical record that preserves a message's prior state when Telegram edits or deletes it; it is distinct from the current searchable message state. The capability is disabled by default and must be requested explicitly when enabled.
