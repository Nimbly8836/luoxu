---
status: accepted
---

# Database-backed access control with an anonymous `pub` role

Luoxu will authenticate named users with an account/password login that issues short-lived JWT access tokens and revocable refresh tokens, while unauthenticated requests act as anonymous visitors rather than as a login account. Named users and the anonymous `pub` role will use the same database-backed group authorization model; deployment configuration may bootstrap the initial administrator and public-group grants, after which permissions are maintained through administrator-only Web APIs. This keeps listing, searching, message details, and message context behind one consistent group-access boundary without requiring an anonymous credential.

## Considered options

- Keep public groups only in configuration: rejected because anonymous access would bypass the database authorization model and its auditability.
- Treat `pub` as a real user account: rejected because it creates misleading credential and lifecycle semantics for unauthenticated requests.
- Allow request-time expansion of public groups: rejected because it risks accidental disclosure of indexed private content.
- Maintain permissions only through a local CLI: rejected because the requested operating model is Web-based administration; the bootstrap administrator is configured with a password hash and then manages permissions through protected APIs.
