# Luoxu message archive

This context defines the people, access boundaries, and Telegram content concepts used by the Luoxu archive.

## Access

**Anonymous visitor**:
A request that has not established a user identity. An anonymous visitor is evaluated through the `pub` access role and never represents a login account.

**Authenticated user**:
A person with a Luoxu login identity. In ordinary Web sessions, non-administrators can access public groups plus explicit grants and active administrators read every archive. Telegram Mini App sessions have the narrower member-intersection scope below, regardless of the linked account's role or grants.

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
An archive explicitly accessible to anonymous Web visitors and authenticated Web users. A Telegram Mini App visitor additionally needs current membership and active indexing references. This is independent of whether the Telegram group has a public username. A group requiring per-account visibility must not receive public access.

**Administrator**:
An active account with the current database `is_admin` role. Through Web sessions, administrators can read all archived groups, topics and private conversations through ordinary content APIs without explicit grants, including stopped archives and future conversations. They can also manage monitoring, accounts and grants. Implicit read access does not publish data or create indexing references. Demotion or disabling takes effect on the next authenticated request; the username or a stale JWT role claim does not confer administrator access.

**Telegram Mini App visitor**:
A server-verified Telegram identity with a distinct scoped session. The configured owner can read groups they currently belong to that are actively indexed; all other visitors additionally require the parent group to be explicitly public. Topics inherit only verified parents. Telegram sessions never inherit Web admin/grant access, cannot use management APIs, and exclude private chats. Eligible unbound visitors receive a non-admin Telegram-only account without grants or a usable password. No eligible groups means silent UI, never anonymous fallback or cached group-name display. Knowing the public Web URL still permits ordinary anonymous Web access.

**Private conversation**:
An indexed one-to-one Telegram conversation. It is denied to anonymous and Mini App visitors; non-administrator Web accounts need an explicit grant, while Web administrators have implicit read access.

## Content

**Conversation**:
A searchable Telegram exchange represented in the archive. A conversation may be a group, a forum topic, or an explicitly indexed one-to-one private chat; each has its own access grants.

**Physical archive**:
One Telegram peer (group/channel or private chat) owns an independent ordinary message table and an optional matching vector table. A group's Topics share its physical table but retain separate conversation access boundaries. There is no global message parent table and no yearly partitioning. Content and sender-name searches must specify one group or conversation; unspecified scope is rejected, not widened to all accessible groups.

**Forum topic**:
A named discussion area in a Telegram group with Topics enabled. Ordinary message replies and their reply threads are not forum topics and remain part of the group conversation.

**Message context**:
The nearby conversation needed to understand a selected message. The chronological window stays within the same topic. Its discussion thread starts from earlier replied-to originals and includes later replies and parallel branches, independently of search matches or the window. Only currently accessible local messages from the same Telegram peer are eligible; inaccessible or missing originals remain opaque placeholders. Depth/count limits and unavailable/deleted content are reported explicitly; local completeness never asserts completeness of Telegram history.

**Message change record**:
A historical record that preserves a message's prior state when Telegram edits or deletes it; it is distinct from the current message state. The capability is disabled by default and must be requested explicitly when enabled.

**Deleted-message search result**:
An explicitly included deleted archive message, showing its saved pre-deletion snapshot when available and otherwise only a deletion marker. It remains subject to current conversation access and never represents restored current content.
