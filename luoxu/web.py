import asyncio
import datetime
import json
import logging
import os
import re
import tempfile
import time
import uuid
from asyncio import Lock
from collections import OrderedDict
from contextlib import suppress
from html import escape as htmlescape
from weakref import WeakValueDictionary

import asyncpg  # type: ignore[import-not-found]
import jwt  # type: ignore[import-not-found]
from aiohttp import web  # type: ignore[import-not-found]
from telethon.errors import RPCError  # type: ignore[import-not-found]
from telethon.tl.types import User  # type: ignore[import-not-found]

from . import util
from .auth import AuthService, Principal  # type: ignore[import-not-found]
from .group import MonitoringUnavailable
from .types import GroupNotFound, SearchQuery

logger = logging.getLogger(__name__)


SWAGGER_TEMPLATE = """<!doctype html><html><head><title>Luoxu API</title></head><body>
<div id="swagger-ui"></div><script src="https://unpkg.com/swagger-ui-dist/swagger-ui-bundle.js"></script>
<script>window.ui=SwaggerUIBundle({url:%s,dom_id:"#swagger-ui"})</script></body></html>"""
OPENAPI_PATH = os.path.abspath(
  os.path.join(os.path.dirname(__file__), "..", "openapi.yaml")
)


def _json_value(value):
  if isinstance(value, (datetime.datetime, datetime.date)):
    return (
      value.timestamp() if isinstance(value, datetime.datetime) else value.isoformat()
    )
  if isinstance(value, uuid.UUID):
    return str(value)
  if isinstance(value, str) and value.startswith("{"):
    try:
      return json.loads(value)
    except json.JSONDecodeError:
      pass
  return value


def _message_json(row, *, include_text=True):
  if isinstance(row, dict) and row.get("status") == "unavailable":
    return row
  deleted = bool(row.get("deleted_at"))
  return {
    "conversation_id": str(row["conversation_id"]),
    "id": row["msgid"],
    "group_id": row.get("group_id"),
    "from_id": row["from_user"],
    "from_name": row["from_user_name"],
    "text": row["text"] if include_text and not deleted else None,
    "html": htmlescape(row["text"]) if include_text and not deleted else None,
    "t": row["created_at"].timestamp(),
    "edited": row["updated_at"].timestamp() if row.get("updated_at") else None,
    "deleted": deleted,
    "deleted_at": row["deleted_at"].timestamp() if row.get("deleted_at") else None,
    "reply_to_id": row.get("reply_to_id"),
    "topic_id": row.get("topic_id"),
    "quote_text": row.get("quote_text"),
    "media": _json_value(row.get("media")),
  }


def _conversation_json(row):
  return {
    "id": str(row["id"]),
    "kind": row["kind"],
    "name": row["name"],
    "telegram_peer_type": row["telegram_peer_type"],
    "telegram_peer_id": row["telegram_peer_id"],
    "topic_id": row["topic_id"],
    "pub_id": row["pub_id"],
    "legacy_group_id": row["legacy_group_id"],
  }


def _user_json(row):
  return {
    "id": str(row["id"]),
    "username": row["username"],
    "is_admin": row["is_admin"],
    "is_active": row["is_active"],
    "created_at": row["created_at"].timestamp(),
    "updated_at": row["updated_at"].timestamp(),
  }


def _bad(message="invalid request"):
  return web.json_response({"error": message}, status=400)


async def _json_body(request):
  try:
    data = await request.json()
  except ValueError as exc:
    raise web.HTTPBadRequest(text="invalid JSON body") from exc
  if not isinstance(data, dict):
    raise web.HTTPBadRequest(text="JSON object required")
  return data


def _int(value, name="parameter"):
  try:
    return int(value)
  except (TypeError, ValueError) as exc:
    raise web.HTTPBadRequest(text=f"invalid {name}") from exc


def _uuid(value, name="conversation id"):
  try:
    return uuid.UUID(str(value))
  except (TypeError, ValueError) as exc:
    raise web.HTTPBadRequest(text=f"invalid {name}") from exc


@web.middleware
async def cors_middleware(request, handler):
  origin = request.headers.get("Origin")
  origins = request.app.get("origins", ())
  if origin and origin not in origins:
    raise web.HTTPForbidden(text="origin is not allowed")
  if request.method == "OPTIONS":
    response = web.Response(status=204)
  else:
    try:
      response = await handler(request)
    except web.HTTPException as exc:
      response = exc
  if origin:
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = (
      "GET, POST, PATCH, PUT, DELETE, OPTIONS"
    )
    response.headers["Vary"] = "Origin, Authorization"
  return response


@web.middleware
async def auth_middleware(request, handler):
  token = request.headers.get("Authorization", "")
  principal = Principal(None, None)
  if token:
    if not token.startswith("Bearer "):
      raise web.HTTPUnauthorized(text="invalid authorization scheme")
    try:
      if request.app["auth"] is None:
        raise jwt.InvalidTokenError("authentication is not configured")
      claims = request.app["auth"].decode(token[7:].strip())
      subject = claims["sub"]
      if not isinstance(subject, str):
        raise jwt.InvalidTokenError("invalid subject")
      subject = str(uuid.UUID(subject))
      user = await request.app["db"].get_user(subject)
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as exc:
      raise web.HTTPUnauthorized(text="invalid access token") from exc
    if not user or not user["is_active"]:
      raise web.HTTPUnauthorized(text="user is inactive")
    principal = Principal(str(user["id"]), user["username"], user["is_admin"], False)
  request["principal"] = principal
  return await handler(request)


class BaseHandler:
  async def _get(self, request):
    raise NotImplementedError

  def __init__(self, dbconn):
    self.dbconn = dbconn

  async def get(self, request):
    started = time.time()
    res = await self._get(request)
    logger.info("request took %.3fs", time.time() - started)
    res.headers.setdefault("Cache-Control", "private, no-store")
    res.headers.setdefault("Vary", "Authorization")
    return res


def html_or_text(m):
  if r := m.get("html"):
    return re.sub(r'<span class="keyword">(\s+)', r'\1<span class="keyword">', r)
  if r := m.get("text"):
    return htmlescape(r)
  return " "


class SearchHandler(BaseHandler):
  async def _get(self, request):
    try:
      q = self._parse_query(request.query)
      groupinfo, messages = await self.dbconn.search(q, request["principal"])
    except (ValueError, TypeError, KeyError) as exc:
      raise web.HTTPBadRequest from exc
    except GroupNotFound as exc:
      raise web.HTTPNotFound from exc
    return web.json_response(
      {
        "groupinfo": groupinfo,
        "has_more": len(messages) == self.dbconn.SEARCH_LIMIT,
        "messages": [
          {
            "id": m["msgid"],
            "conversation_id": str(m["conversation_id"]),
            "from_id": m["from_user"],
            "from_name": m["from_user_name"],
            "group_id": m["group_id"],
            "html": html_or_text(m),
            "t": m["created_at"].timestamp(),
            "edited": m["updated_at"].timestamp() if m["updated_at"] else None,
          }
          for m in messages
        ],
      },
      headers={"Cache-Control": "max-age=0"},
    )

  def _parse_query(self, query):
    group = _int(query.get("g", 0), "group")
    conversation_id = query.get("conversation_id") or None
    if conversation_id:
      _uuid(conversation_id)
    terms = query.get("q")
    sender = self._parse_sender(query.get("sender"))
    start = (
      util.fromtimestamp(_int(query["start"], "start")) if query.get("start") else None
    )
    end = util.fromtimestamp(_int(query["end"], "end")) if query.get("end") else None
    exclude_sender = self._parse_sender(query.get("exclude_sender"))
    return SearchQuery(group, terms, sender, start, end, conversation_id, exclude_sender)

  @staticmethod
  def _parse_sender(sender):
    if not sender:
      return None
    parsed = [_int(s, "sender") for s in sender.split(",") if s.strip()]
    return [s for s in parsed if s] or None


class GroupsHandler(BaseHandler):
  async def _get(self, request):
    groups = await self.dbconn.get_groups(request["principal"])
    gs = [
      {
        "group_id": str(g["group_id"]),
        "name": g["name"],
        "pub_id": g["pub_id"],
        "conversation_id": str(g["conversation_uuid"]),
      }
      for g in groups
    ]
    gs.sort(key=lambda g: g["name"])
    return web.json_response({"groups": gs})


class NamesHandler(BaseHandler):
  async def _get(self, request):
    group = _int(request.query.get("g") or 0, "group")
    query = request.query.get("q")
    if query is None:
      raise web.HTTPBadRequest(text="q is required")
    names = await self.dbconn.find_names(group, query, request["principal"])
    return web.json_response(
      {"names": names}, headers={"Cache-Control": "private, no-store"}
    )


class ConversationHandler(BaseHandler):
  async def _get(self, request):
    rows = await self.dbconn.list_conversations(request["principal"])
    return web.json_response({"conversations": [_conversation_json(r) for r in rows]})


class MessageHandler(BaseHandler):
  async def _get(self, request):
    conversation_id = self._uuid(request)
    msg = await self.dbconn.get_message(
      conversation_id,
      _int(request.match_info["msgid"], "message id"),
      request["principal"],
    )
    if not msg:
      raise web.HTTPNotFound
    return web.json_response({"message": _message_json(msg)})

  @staticmethod
  def _uuid(request):
    try:
      return uuid.UUID(request.match_info["conversation_id"])
    except ValueError as exc:
      raise web.HTTPBadRequest(text="invalid conversation id") from exc


class ContextHandler(MessageHandler):
  async def _target_ids(self, request):
    if "conversation_id" in request.match_info:
      return self._uuid(request), _int(request.match_info["msgid"], "message id")
    group_id = _int(request.query.get("g"), "group")
    msgid = _int(request.query.get("id"), "message id")
    if not 0 < group_id < 2**63 or not 0 < msgid < 2**63:
      raise web.HTTPBadRequest(text="g and id must be positive 64-bit integers")
    cid = await self.dbconn.find_group_message_conversation(
      group_id, msgid, request["principal"]
    )
    if cid is None:
      raise web.HTTPNotFound
    return cid, msgid

  async def _get(self, request):
    cid, msgid = await self._target_ids(request)
    config = request.app["context"]
    try:
      before = min(
        _int(request.query.get("before", config["before"]), "before"),
        config["before"],
      )
      after = min(
        _int(request.query.get("after", config["after"]), "after"),
        config["after"],
      )
      if before + after > config["max_window"]:
        raise web.HTTPBadRequest(text="context window is too large")
      depth = min(
        _int(request.query.get("depth", config["reply_depth"]), "depth"),
        config["reply_depth"],
      )
      reply_limit = min(
        _int(
          request.query.get("reply_limit", config["reply_limit"]),
          "reply_limit",
        ),
        config["reply_limit"],
      )
    except ValueError as exc:
      raise web.HTTPBadRequest from exc
    if min(before, after, depth, reply_limit) < 0:
      raise web.HTTPBadRequest
    context = await self.dbconn.get_context(
      cid,
      msgid,
      request["principal"],
      before,
      after,
      depth,
      reply_limit,
    )
    if not context:
      raise web.HTTPNotFound
    rows = [context["target"], *context["replies"]]
    unavailable = sum(r.get("status") == "unavailable" for r in rows)
    deleted = sum(bool(r.get("deleted_at")) for r in rows)
    truncated = context.get("replies_truncated", False)
    return web.json_response(
      {
        "target": _message_json(context["target"]),
        "before": [_message_json(m) for m in context["before"]],
        "after": [_message_json(m) for m in context["after"]],
        "replies": [_message_json(m) for m in context["replies"]],
        "replies_meta": {
          "scope": "accessible_local_archive",
          "complete": not (truncated or unavailable or deleted),
          "truncated": truncated,
          "unavailable_count": unavailable,
          "deleted_count": deleted,
          "depth": depth,
          "limit": reply_limit,
        },
      }
    )


class HistoryHandler(MessageHandler):
  async def _get(self, request):
    if not request.app["history_enabled"]:
      raise web.HTTPNotFound
    if request.query.get("include_history", "").lower() != "true":
      return web.json_response({"history": []})
    cid = self._uuid(request)
    rows = await self.dbconn.get_revisions(
      cid, _int(request.match_info["msgid"], "message id"), request["principal"]
    )
    if not rows:
      raise web.HTTPNotFound
    return web.json_response(
      {
        "history": [
          {
            "id": r["id"],
            "type": r["revision_type"],
            "text": r["text"],
            "from_id": r["from_user"],
            "from_name": r["from_user_name"],
            "reply_to_id": r["reply_to_id"],
            "topic_id": r["topic_id"],
            "quote_text": r["quote_text"],
            "media": _json_value(r["media"]),
            "created_at": r["created_at"].timestamp(),
            "edited_at": r["updated_at"].timestamp() if r["updated_at"] else None,
            "deleted_at": r["deleted_at"].timestamp() if r["deleted_at"] else None,
            "captured_at": r["captured_at"].timestamp(),
          }
          for r in rows
        ]
      }
    )


class AuthHandler:
  def __init__(self, db, auth):
    self.db, self.auth = db, auth

  async def login(self, request):
    data = await _json_body(request)
    user = await self.db.get_user_by_username(str(data.get("username", "")))
    if (
      not user
      or not user["is_active"]
      or not await asyncio.to_thread(
        self.auth.verify_password,
        user["password_hash"],
        str(data.get("password", "")),
      )
    ):
      raise web.HTTPUnauthorized(text="invalid username or password")
    return web.json_response(await self._tokens(user))

  async def refresh(self, request):
    data = await _json_body(request)
    raw = str(data.get("refresh_token", ""))
    fingerprint = None
    try:
      claims = self.auth.decode(raw, "refresh")
      fingerprint = self.auth.token_fingerprint(claims["jti"])
      user = await self.db.consume_refresh_token(fingerprint)
    except (jwt.InvalidTokenError, KeyError, TypeError):
      user = None
    if not user or not user["is_active"] or not fingerprint:
      raise web.HTTPUnauthorized(text="invalid refresh token")
    return web.json_response(await self._tokens(user))

  async def me(self, request):
    principal = request["principal"]
    if principal.is_anonymous:
      raise web.HTTPUnauthorized
    user = await self.db.get_user(principal.user_id)
    if not user or not user["is_active"]:
      raise web.HTTPUnauthorized
    return web.json_response({"user": _user_json(user)})

  async def revoke_sessions(self, request):
    principal = request["principal"]
    if principal.is_anonymous:
      raise web.HTTPUnauthorized
    await self.db.revoke_user_sessions(principal.user_id)
    return web.Response(status=204)

  async def _tokens(self, user):
    access = self.auth.access_token(user)
    refresh, jti, ttl = self.auth.refresh_token(user)
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
      seconds=ttl
    )
    await self.db.save_refresh_token(
      user["id"], self.auth.token_fingerprint(jti), expires
    )
    return {
      "access_token": access,
      "refresh_token": refresh,
      "token_type": "Bearer",
      "expires_in": self.auth.access_ttl,
    }


async def require_admin(request):
  principal = request["principal"]
  if principal.is_anonymous:
    raise web.HTTPUnauthorized(text="authentication required")
  if not principal.is_admin:
    raise web.HTTPForbidden(text="administrator access required")


class AdminHandler:
  def __init__(
    self, db, auth, add_group=None, monitoring_changed=None, monitoring_status=None
  ):
    self.db, self.auth, self.group_adder = db, auth, add_group
    self.monitoring_changed = monitoring_changed
    self.monitoring_status = monitoring_status

  async def _refresh_monitoring(self):
    if self.monitoring_changed is not None:
      try:
        await self.monitoring_changed()
      except Exception:
        # The reference mutation is already committed. Periodic reconciliation
        # retries it; a transient local worker error must not undo valid grants.
        logger.exception("monitoring references saved; runtime refresh will retry")

  async def users(self, request):
    await require_admin(request)
    return web.json_response(
      {"users": [_user_json(u) for u in await self.db.list_users()]}
    )

  async def _public_conversation_ids(self):
    # Reuse effective anonymous access, including inherited public forum topics.
    rows = await self.db.list_conversations(Principal(None, None))
    return {row["id"] for row in rows}

  async def conversations(self, request):
    return await self._conversation_list(request, monitored_only=False)

  async def groups(self, request):
    return await self._conversation_list(request, monitored_only=True)

  async def _conversation_list(self, request, *, monitored_only):
    await require_admin(request)
    monitoring = await self.db.list_group_monitoring()
    by_peer = {(r["telegram_peer_type"], r["telegram_peer_id"]): r for r in monitoring}
    rows = monitoring if monitored_only else await self.db.list_all_conversations()
    public_ids = await self._public_conversation_ids()
    runtime = self.monitoring_status() if self.monitoring_status else None
    result = []
    for row in rows:
      state = by_peer.get((row["telegram_peer_type"], row["telegram_peer_id"]))
      if monitored_only and (state is None or not state["reference_count"]):
        continue
      item = {**_conversation_json(row), "is_public": row["id"] in public_ids}
      if state is not None:
        default_state = "pending" if state["reference_count"] else "stopped"
        item["monitoring"] = {
          "requested": state["reference_count"] > 0,
          "manual": state["manual_reference"],
          "account_references": state["user_references"],
          "public_references": state["public_references"],
          "reference_count": state["reference_count"],
          "runtime": runtime.get(
            str(state["id"]), {"state": default_state, "error_type": None}
          )
          if runtime is not None
          else {"state": "unknown", "error_type": None},
        }
      result.append(item)
    return web.json_response(
      {"groups" if monitored_only else "conversations": result},
      headers={"Cache-Control": "private, no-store"},
    )

  async def manual_monitoring(self, request):
    await require_admin(request)
    found = await self.db.set_manual_monitoring(
      _uuid(request.match_info["conversation_id"]),
      request.method == "PUT",
    )
    if not found:
      raise web.HTTPNotFound(text="group conversation not found")
    await self._refresh_monitoring()
    return web.Response(status=204)

  async def user_grants(self, request):
    await require_admin(request)
    user_id = _uuid(request.match_info["user_id"], "user id")
    if not await self.db.get_user(user_id):
      raise web.HTTPNotFound
    return web.json_response(
      {
        "conversations": [
          _conversation_json(r) for r in await self.db.list_user_grants(user_id)
        ]
      }
    )

  async def public_grants(self, request):
    await require_admin(request)
    return web.json_response(
      {
        "conversations": [
          _conversation_json(r) for r in await self.db.list_public_grants()
        ]
      }
    )

  async def create_user(self, request):
    await require_admin(request)
    data = await _json_body(request)
    try:
      username = data["username"]
      password = data["password"]
      is_admin = data.get("is_admin", False)
      if not isinstance(username, str) or not isinstance(password, str):
        raise TypeError("username and password must be strings")
      if not isinstance(is_admin, bool):
        raise TypeError("is_admin must be boolean")
      user = await self.db.create_user(
        username,
        await asyncio.to_thread(self.auth.hash_password, password),
        is_admin,
      )
    except (KeyError, TypeError, ValueError) as exc:
      raise web.HTTPBadRequest(text="username and password are required") from exc
    except asyncpg.UniqueViolationError as exc:
      raise web.HTTPConflict(text="username already exists") from exc
    return web.json_response({"user": _user_json(user)}, status=201)

  async def update_user(self, request):
    await require_admin(request)
    data = await _json_body(request)
    kwargs = {}
    try:
      if "password" in data:
        if not isinstance(data["password"], str):
          raise ValueError("password must be a string")
        kwargs["password_hash"] = await asyncio.to_thread(
          self.auth.hash_password, data["password"]
        )
      for key in ("is_active", "is_admin"):
        if key in data:
          if not isinstance(data[key], bool):
            raise ValueError(f"{key} must be boolean")
          kwargs[key] = data[key]
    except ValueError as exc:
      raise web.HTTPBadRequest(text=str(exc)) from exc
    try:
      user = await self.db.update_user(
        _uuid(request.match_info["user_id"], "user id"), **kwargs
      )
    except ValueError as exc:
      raise web.HTTPConflict(text=str(exc)) from exc
    if not user:
      raise web.HTTPNotFound
    if "password" in data or not data.get("is_active", True):
      await self.db.revoke_user_sessions(user["id"])
    return web.json_response({"user": _user_json(user)})

  async def delete_user(self, request):
    await require_admin(request)
    user_id = _uuid(request.match_info["user_id"], "user id")
    try:
      found = await self.db.delete_user(user_id)
    except ValueError as exc:
      raise web.HTTPConflict(text=str(exc)) from exc
    if not found:
      raise web.HTTPNotFound
    await self._refresh_monitoring()
    return web.Response(status=204)

  async def grant(self, request):
    await require_admin(request)
    user_id = _uuid(request.match_info["user_id"], "user id")
    conversation_id = _uuid(request.match_info["conversation_id"])
    try:
      await self.db.grant_conversation(user_id, conversation_id)
    except ValueError as exc:
      raise web.HTTPNotFound(text=str(exc)) from exc
    await self._refresh_monitoring()
    return web.Response(status=204)

  async def revoke(self, request):
    await require_admin(request)
    await self.db.revoke_conversation(
      _uuid(request.match_info["user_id"], "user id"),
      _uuid(request.match_info["conversation_id"]),
    )
    await self._refresh_monitoring()
    return web.Response(status=204)

  async def public_grant(self, request):
    await require_admin(request)
    try:
      await self.db.grant_public(_uuid(request.match_info["conversation_id"]))
    except ValueError as exc:
      raise web.HTTPBadRequest(text=str(exc)) from exc
    await self._refresh_monitoring()
    return web.Response(status=204)

  async def add_group(self, request):
    await require_admin(request)
    if self.group_adder is None:
      raise web.HTTPServiceUnavailable(text="group monitoring is unavailable")
    data = await _json_body(request)
    target = data.get("group")
    if (
      isinstance(target, bool)
      or not isinstance(target, (str, int))
      or not str(target).strip()
    ):
      raise web.HTTPBadRequest(text="group is required")
    try:
      group = await self.group_adder(str(target).strip())
    except MonitoringUnavailable as exc:
      raise web.HTTPServiceUnavailable(text=str(exc)) from exc
    except (TypeError, ValueError) as exc:
      raise web.HTTPBadRequest(text="invalid group") from exc
    # The indexer returns legacy group state with history cursors, not a
    # conversation row. Resolve its UUID instead of serializing missing fields.
    conversation = await self.db.get_conversation(group["conversation_uuid"])
    if conversation is None:
      raise web.HTTPServiceUnavailable(text="group conversation is unavailable")
    public_ids = await self._public_conversation_ids()
    return web.json_response(
      {
        "conversation": {
          **_conversation_json(conversation),
          "is_public": conversation["id"] in public_ids,
        }
      },
      status=201,
      headers={"Cache-Control": "private, no-store"},
    )

  async def public_revoke(self, request):
    await require_admin(request)
    await self.db.revoke_public(_uuid(request.match_info["conversation_id"]))
    await self._refresh_monitoring()
    return web.Response(status=204)

  async def revoke_sessions(self, request):
    await require_admin(request)
    await self.db.revoke_user_sessions(_uuid(request.match_info["user_id"], "user id"))
    return web.Response(status=204)


class AvatarHandler:
  FETCH_TIMEOUT = 3.0  # HTTP wait only; expiry must not cancel cache population.
  LOAD_TIMEOUT = 30.0
  CACHE_SECONDS = 300.0
  MAX_CACHED_USERS = 1024
  MAX_PENDING = 128
  FAILURE_RETRY_SECONDS = 30.0
  MAX_FAILURES = 1024
  MAX_DOWNLOADS = 4

  def __init__(
    self, client, db, cache_dir, default_avatar: str, ghost_avatar: str
  ) -> None:
    self.client, self.db, self.cache_dir = client, db, cache_dir
    self.default_avatar, self.ghost_avatar = default_avatar, ghost_avatar
    self._photo_locks: WeakValueDictionary[int, Lock] = WeakValueDictionary()
    self._downloads = asyncio.Semaphore(self.MAX_DOWNLOADS)
    self._retry_after: OrderedDict[int, float] = OrderedDict()
    self._known: OrderedDict[int, tuple[str, float]] = OrderedDict()
    self._pending: dict[int, asyncio.Task[str]] = {}
    self._closed = False

  async def _get_avatar(self, user: User) -> str:
    photo_id = getattr(getattr(user, "photo", None), "photo_id", None)
    if photo_id is None:
      raise ValueError("user has no downloadable avatar")
    file = os.path.join(self.cache_dir, f"{photo_id}.jpg")
    if os.path.exists(file):
      return file
    # Only the download path owns this lock. Strong local references keep it
    # alive while any request waits; unused locks disappear from the weak map.
    lock = self._photo_locks.get(photo_id)
    if lock is None:
      self._photo_locks[photo_id] = lock = Lock()
    async with lock:
      if os.path.exists(file):
        return file
      # Different photos/processes must not share a fixed tmp.jpg. Publish
      # only a completed nonempty download, and clean up even on cancellation.
      with tempfile.NamedTemporaryFile(
        dir=self.cache_dir,
        prefix=f".avatar-{photo_id}-",
        suffix=".jpg",
        delete=False,
      ) as temporary:
        tmpfile = temporary.name
      try:
        downloaded = await self.client.download_profile_photo(user, file=tmpfile)
        if not downloaded or os.path.getsize(tmpfile) == 0:
          raise ValueError("avatar download returned no photo")
        os.replace(tmpfile, file)
      finally:
        with suppress(FileNotFoundError):
          os.unlink(tmpfile)
    return file

  def _cached_file(self, uid: int) -> tuple[str, float] | None:
    cached = self._known.get(uid)
    if cached and os.path.isfile(cached[0]):
      self._known.move_to_end(uid)
      return cached
    self._known.pop(uid, None)
    return None

  async def _load_avatar(self, uid: int) -> str:
    stage = "queue"
    try:
      # One limit for entity RPCs AND downloads, independent of HTTP lifetime.
      async with asyncio.timeout(self.LOAD_TIMEOUT), self._downloads:
        stage = "entity"
        user = await self.client.get_entity(uid)
        if getattr(user, "deleted", False):
          file = self.ghost_avatar
        elif getattr(getattr(user, "photo", None), "photo_id", None) is None:
          file = self.default_avatar
        else:
          stage = "download"
          file = await self._get_avatar(user)
      self._known[uid] = (file, time.monotonic() + self.CACHE_SECONDS)
      self._known.move_to_end(uid)
      if len(self._known) > self.MAX_CACHED_USERS:
        self._known.popitem(last=False)
      self._retry_after.pop(uid, None)
      return file
    except (TimeoutError, OSError, ValueError, RPCError) as exc:
      # Only a failed BACKGROUND load starts backoff, not a short HTTP wait.
      self._retry_after[uid] = time.monotonic() + self.FAILURE_RETRY_SECONDS
      self._retry_after.move_to_end(uid)
      if len(self._retry_after) > self.MAX_FAILURES:
        self._retry_after.popitem(last=False)
      logger.warning(
        "avatar fetch for user %s failed at %s (%s); using cached/default image",
        uid,
        stage,
        type(exc).__name__,
      )
      cached = self._cached_file(uid)
      return cached[0] if cached else self.default_avatar

  def _load_done(self, uid: int, task: asyncio.Task[str]) -> None:
    self._pending.pop(uid, None)
    # Observe unexpected failures even if every HTTP waiter has already left.
    if not task.cancelled() and (exc := task.exception()) is not None:
      logger.error(
        "unexpected avatar load failure for user %s",
        uid,
        exc_info=(type(exc), exc, exc.__traceback__),
      )

  async def _avatar_file(self, uid: int) -> str:
    cached = self._cached_file(uid)
    now = time.monotonic()
    if cached and cached[1] > now:
      return cached[0]
    fallback = cached[0] if cached else self.default_avatar
    if self._closed or self._retry_after.get(uid, 0) > now:
      return fallback
    self._retry_after.pop(uid, None)
    task = self._pending.get(uid)
    if task is None:
      if len(self._pending) >= self.MAX_PENDING:
        return fallback
      task = asyncio.create_task(self._load_avatar(uid), name=f"avatar-{uid}")
      self._pending[uid] = task
      task.add_done_callback(lambda done: self._load_done(uid, done))
    if cached:
      # Revalidate expired metadata without replacing a known photo with default.
      return cached[0]
    try:
      return await asyncio.wait_for(asyncio.shield(task), self.FETCH_TIMEOUT)
    except TimeoutError:
      return fallback

  def _status(self, uid: int, file: str) -> str:
    if file not in (self.default_avatar, self.ghost_avatar):
      return "ready"
    if uid in self._pending:
      return "pending"
    if self._retry_after.get(uid, 0) > time.monotonic():
      return "unavailable"
    if uid not in self._known:
      return "unavailable"  # queue full or application shutting down
    return "deleted" if file == self.ghost_avatar else "missing"

  async def close(self, _app=None) -> None:
    self._closed = True
    tasks = tuple(self._pending.values())
    for task in tasks:
      task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    self._pending.clear()

  async def get(self, request) -> web.FileResponse:
    if uid_str := request.match_info.get("uid"):
      uid = _int(uid_str, "user id")
      # Authorization must precede disk-cache hits and failure-backoff hits.
      if not await self.db.can_view_user(uid, request["principal"]):
        raise web.HTTPNotFound
      file = await self._avatar_file(uid)
      avatar_headers = {"X-Luoxu-Avatar-Status": self._status(uid, file)}
      name, cache_control = uid_str, "private, no-store"
    elif name := request.match_info.get("name"):
      avatar_headers = {}
      cache_control = f"public, max-age={86400 * 365}"
      if name == "ghost":
        file = self.ghost_avatar
      elif name == "nobody":
        file = self.default_avatar
      else:
        raise web.HTTPNotFound
    else:
      raise web.HTTPNotFound
    return web.FileResponse(
      path=file,
      headers={
        "Vary": "Authorization",
        "Content-Type": "image/jpeg",
        "Cache-Control": cache_control,
        "Content-Disposition": f'inline; filename="avatar-{name}.jpg"',
        **avatar_headers,
      },
    )


def setup_app(
  dbconn,
  client,
  cache_dir,
  default_avatar,
  ghost_avatar,
  *,
  prefix="",
  origins=(),
  auth_service=None,
  history_enabled=False,
  context_config=None,
  add_group=None,
  monitoring_changed=None,
  monitoring_status=None,
):
  app = web.Application(middlewares=[cors_middleware, auth_middleware])
  app["origins"] = origins
  app["db"], app["auth"] = dbconn, auth_service
  app["history_enabled"] = history_enabled
  app["context"] = {
    "before": 5,
    "after": 5,
    "max_window": 20,
    "reply_depth": 5,
    "reply_limit": 100,
    **(context_config or {}),
  }
  app.router.add_get(f"{prefix}/search", SearchHandler(dbconn).get)
  app.router.add_get(f"{prefix}/context", ContextHandler(dbconn).get)
  app.router.add_get(f"{prefix}/groups", GroupsHandler(dbconn).get)
  app.router.add_get(f"{prefix}/names", NamesHandler(dbconn).get)
  app.router.add_get(f"{prefix}/conversations", ConversationHandler(dbconn).get)
  app.router.add_get(
    f"{prefix}/conversations/{{conversation_id}}/messages/{{msgid:\\d+}}",
    MessageHandler(dbconn).get,
  )
  app.router.add_get(
    f"{prefix}/conversations/{{conversation_id}}/messages/{{msgid:\\d+}}/context",
    ContextHandler(dbconn).get,
  )
  app.router.add_get(
    f"{prefix}/conversations/{{conversation_id}}/messages/{{msgid:\\d+}}/history",
    HistoryHandler(dbconn).get,
  )
  auth = AuthHandler(dbconn, auth_service)
  app.router.add_post(f"{prefix}/auth/login", auth.login)
  app.router.add_post(f"{prefix}/auth/refresh", auth.refresh)
  app.router.add_get(f"{prefix}/auth/me", auth.me)
  app.router.add_post(f"{prefix}/auth/sessions/revoke", auth.revoke_sessions)
  admin = AdminHandler(
    dbconn, auth_service, add_group, monitoring_changed, monitoring_status
  )
  app.router.add_get(f"{prefix}/admin/users", admin.users)
  app.router.add_get(f"{prefix}/admin/conversations", admin.conversations)
  app.router.add_get(f"{prefix}/admin/users/{{user_id}}/grants", admin.user_grants)
  app.router.add_get(f"{prefix}/admin/public", admin.public_grants)
  app.router.add_post(f"{prefix}/admin/users", admin.create_user)
  app.router.add_patch(f"{prefix}/admin/users/{{user_id}}", admin.update_user)
  app.router.add_delete(f"{prefix}/admin/users/{{user_id}}", admin.delete_user)
  app.router.add_put(
    f"{prefix}/admin/users/{{user_id}}/grants/{{conversation_id}}", admin.grant
  )
  app.router.add_delete(
    f"{prefix}/admin/users/{{user_id}}/grants/{{conversation_id}}", admin.revoke
  )
  app.router.add_post(f"{prefix}/admin/groups", admin.add_group)
  app.router.add_get(f"{prefix}/admin/groups", admin.groups)
  app.router.add_put(
    f"{prefix}/admin/groups/{{conversation_id}}", admin.manual_monitoring
  )
  app.router.add_delete(
    f"{prefix}/admin/groups/{{conversation_id}}", admin.manual_monitoring
  )
  app.router.add_post(f"{prefix}/admin/public/{{conversation_id}}", admin.public_grant)
  app.router.add_delete(
    f"{prefix}/admin/public/{{conversation_id}}", admin.public_revoke
  )
  app.router.add_post(
    f"{prefix}/admin/users/{{user_id}}/sessions/revoke", admin.revoke_sessions
  )
  spec_url = json.dumps(f"{prefix}/openapi.yaml")

  async def docs(_request):
    return web.Response(text=SWAGGER_TEMPLATE % spec_url, content_type="text/html")

  async def openapi(_request):
    return web.FileResponse(OPENAPI_PATH)

  app.router.add_get(f"{prefix}/docs", docs)
  app.router.add_get(f"{prefix}/openapi.yaml", openapi)
  if client:
    ah = AvatarHandler(client, dbconn, cache_dir, default_avatar, ghost_avatar)
    app.router.add_get(rf"{prefix}/avatar/{{uid:\d+}}.jpg", ah.get)
    app.router.add_get(rf"{prefix}/avatar/{{name:\w+}}.jpg", ah.get)
    app.on_cleanup.append(ah.close)
  return app


async def run_web(config, port):
  from .db import PostgreStore

  db = PostgreStore(
    config["database"],
    None,
    history_enabled=config["web"].get("message_history", {}).get("enabled", False),
  )
  await db.setup()
  web_config = config["web"]
  auth_config = web_config.get("auth", {})
  auth = AuthService(auth_config)
  await db.bootstrap(auth_config, web_config.get("public_groups", ()), auth)
  cache_dir = web_config["cache_dir"]
  try:
    os.makedirs(cache_dir, exist_ok=True)
  except OSError as exc:
    logger.exception("cannot create avatar cache directory")
    raise RuntimeError("cannot create avatar cache directory") from exc
  app = setup_app(
    db,
    None,
    os.path.abspath(cache_dir),
    os.path.abspath(web_config["default_avatar"]),
    os.path.abspath(web_config["ghost_avatar"]),
    prefix=web_config["prefix"],
    origins=web_config["origins"],
    auth_service=auth,
    history_enabled=db.history_enabled,
    context_config=web_config.get("context"),
  )
  runner = web.AppRunner(app)
  await runner.setup()
  site = web.TCPSite(runner, web_config["listen_host"], port)
  await site.start()
  try:
    while True:
      await asyncio.sleep(3600)
  finally:
    await db.close()


if __name__ == "__main__":
  import argparse

  from .lib.nicelogger import enable_pretty_logging
  from .util import load_config, run_until_sigint

  enable_pretty_logging(logging.DEBUG)
  parser = argparse.ArgumentParser()
  parser.add_argument("--config", default="config.toml")
  parser.add_argument("--port", type=int)
  args = parser.parse_args()
  config = load_config(args.config)
  run_until_sigint(run_web(config, args.port))
