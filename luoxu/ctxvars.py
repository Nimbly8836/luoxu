from contextvars import ContextVar

msg_source: ContextVar[str | None] = ContextVar('msg_source', default=None)
group_title: ContextVar[str | None] = ContextVar('group_title', default=None)
