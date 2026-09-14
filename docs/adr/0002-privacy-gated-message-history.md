---
status: accepted
---

# Privacy-gated message history

Message edits and deletions will be captured only while the history feature is enabled; it is disabled by default. When enabled, each edit preserves a complete prior snapshot and each deletion preserves the complete pre-deletion snapshot, while ordinary search continues to use only the current message state. History is returned only when the caller has access to the conversation and explicitly requests it with `include_history=true`; this makes sensitive historical content opt-in at both deployment and request time.

Deletion capture is real-time from the point the feature is enabled, so edits or deletions that happened before enablement or while the indexer was offline are not reconstructed.
