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
A Telegram group selected for ongoing message collection while indexing references remain. Monitoring does not itself make the archive public or grant an account access.

**Indexing reference**:
A retained requirement to collect messages for a group: an independent manual selection, an explicit individual grant, or an explicit public grant on the group or one of its topics. Multiple references share one group archive; withdrawing the last reference stops collection without deleting that archive. References select collection, not additional read access.

**Stopped group archive**:
A retained group archive with no indexing references. Earlier messages remain stored, but the group is no longer selected for further collection.

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
The nearby conversation needed to understand a selected message. The chronological window stays within the same topic. Its discussion thread starts from earlier replied-to originals and includes later replies and parallel branches, independently of search matches or the window. Only currently accessible local messages from the same Telegram peer are eligible; inaccessible or missing originals remain opaque placeholders. Depth/count limits and unavailable/deleted content are reported explicitly; local completeness never asserts completeness of Telegram history.

**Message change record**:
A historical record that preserves a message's prior state when Telegram edits or deletes it; it is distinct from the current searchable message state. The capability is disabled by default and must be requested explicitly when enabled.
