import datetime
from typing import NamedTuple


class SearchQuery(NamedTuple):
  group: int
  terms: str | None
  sender: list[int] | None
  start: datetime.datetime | None
  end: datetime.datetime | None
  conversation_id: str | None = None
  exclude_sender: list[int] | None = None


class GroupNotFound(Exception):
  def __init__(self, group):
    self.group = group

  def __str__(self):
    return f"no such group indexed: {self.group}"
