import asyncio
import logging

import telethon
from telethon.tl import types

import querytrans

logger = logging.getLogger(__name__)

_query_transform = getattr(querytrans, "transform", None)


def text_to_query(s):
  if _query_transform is None:
    # Development installations may not have the optional Rust extension yet.
    # Quote the complete query rather than passing user syntax through.
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
  return _query_transform(s)


async def format_msg(msg, ocrsvc=None) -> str | None:
  try:
    return await asyncio.wait_for(_format_msg(msg, ocrsvc=ocrsvc), 60)
  except asyncio.TimeoutError:
    logger.error("timed out formatting a message: %r", msg.to_dict())


async def _format_msg(msg, ocrsvc=None) -> str | None:
  if isinstance(msg, telethon.tl.patched.MessageService):
    # pinning or joining messages etc
    return None

  text = []

  if m := msg.message:
    text.append(m)

  if p := msg.poll:
    poll_text = "\n".join(a.text.text for a in p.poll.answers)
    text.append(f"[poll] {p.poll.question}\n{poll_text}")

  if w := msg.web_preview:
    text.extend(
      (
        "[webpage]",
        w.url,
        w.site_name,
        w.title,
        w.description,
      )
    )

  if d := msg.document:
    for a in d.attributes:
      if hasattr(a, "file_name"):
        text.append(f"[file] {a.file_name}")
      if getattr(a, "performer", None) and getattr(a, "title", None):
        text.append(f"[audio] {a.title} - {a.performer}")

  if (
    ocrsvc
    and (media := msg.media)
    and (
      isinstance(media, types.MessageMediaPhoto)
      or (
        isinstance(media, types.MessageMediaDocument)
        and msg.media.document.mime_type.startswith("image/")
      )
    )
  ):
    try:
      if ocr_text := await ocrsvc.ocr_img(media):
        text.append("[image]")
        text.extend(ocr_text)
    except Exception as exc:  # noqa: BLE001 - OCR backends expose varied errors
      logger.error("failed to do ocr: %r", exc)

  text = "\n".join(x for x in text if x)

  return text
