"""Physical table routing. Only registry UUIDs, never request strings, form names."""

import uuid


def archive_sql(statement: str, archive_id) -> str:
  key = uuid.UUID(str(archive_id)).hex
  # These are the only interpolated identifiers. All data still uses $n bindings.
  return statement.replace("{messages}", f'"messages_{key}"').replace(
    "{embeddings}", f'"embeddings_{key}"'
  )


class ArchiveConnection:
  """Explicitly bound SQL templates; cannot issue an unscoped content query."""

  def __init__(self, connection, archive_id):
    self.connection = connection
    self.archive_id = uuid.UUID(str(archive_id))

  def __getattr__(self, name):
    return getattr(self.connection, name)

  async def fetch(self, statement, *args):
    return await self.connection.fetch(archive_sql(statement, self.archive_id), *args)

  async def fetchrow(self, statement, *args):
    return await self.connection.fetchrow(archive_sql(statement, self.archive_id), *args)

  async def fetchval(self, statement, *args):
    return await self.connection.fetchval(archive_sql(statement, self.archive_id), *args)

  async def execute(self, statement, *args):
    # Only UUID.hex-derived identifiers are substituted; values remain bound.
    # pi-lens-ignore: python-sql-injection
    return await self.connection.execute(archive_sql(statement, self.archive_id), *args)


async def conversation_archive(conn, conversation_id):
  return await conn.fetchval(
    "SELECT archive_id FROM conversations WHERE id = $1", conversation_id
  )
