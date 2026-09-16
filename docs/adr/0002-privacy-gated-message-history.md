---
status: accepted
---

# Privacy-gated message history

Message edits and deletions will be captured only while the history feature is enabled; it is disabled by default. When enabled, each edit preserves a complete prior snapshot and each deletion preserves the complete pre-deletion snapshot, while default search continues to use only the current non-deleted message state. History is returned only when the caller has current access to the conversation and explicitly opts in; this makes sensitive historical content opt-in at both deployment and request time.

The history endpoint requires `include_history=true`. Search additionally supports the explicit `include_deleted=true` option: when history is enabled, it may search and display the latest saved deletion snapshot for each exact archived message variant, clearly labeled as deleted historical content. It does not search arbitrary edit revisions, restore current content, broaden grants, or reconstruct missing snapshots. Without this option, search behavior is unchanged. See [deleted-message search](../deleted-message-search.md).

Deletion capture is real-time from the point the feature is enabled, so edits or deletions that happened before enablement or while the indexer was offline are not reconstructed.
