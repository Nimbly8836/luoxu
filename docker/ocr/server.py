import asyncio
import json
import logging
import os
import socket
import tempfile
from pathlib import Path

from aiohttp import web  # type: ignore[import-not-found]
from paddleocr import PaddleOCR  # type: ignore[import-not-found]

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

try:
  MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
except ValueError as exc:
  raise RuntimeError("MAX_UPLOAD_BYTES must be an integer") from exc
if MAX_UPLOAD_BYTES <= 0:
  raise RuntimeError("MAX_UPLOAD_BYTES must be positive")
ocr = PaddleOCR(
  lang=os.getenv("PADDLEOCR_LANG", "ch"),
  device=os.getenv("PADDLEOCR_DEVICE", "cpu"),
)
inference_lock = asyncio.Lock()


async def health(request):
  return web.json_response({"status": "ok"})


def _page_text(page):
  data = getattr(page, "json", None)
  if callable(data):
    data = data()
  if isinstance(data, str):
    try:
      data = json.loads(data)
    except json.JSONDecodeError:
      return []
  if not isinstance(data, dict):
    return []
  result = data.get("res", data)
  if not isinstance(result, dict):
    return []
  values = result.get("rec_texts", [])
  return values if isinstance(values, list) else []


def _predict(path):
  # PaddleOCR may return a generator. Consume it here, while the tempfile exists.
  return list(ocr.predict(path))


async def recognize(request):
  reader = await request.multipart()
  upload = await reader.next()
  if upload is None or upload.name != "file":
    raise web.HTTPBadRequest(text='multipart field "file" is required')

  filename = None
  size = 0
  try:
    suffix = Path(upload.filename or ".image").suffix[:16]
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as output:
      filename = output.name
      while chunk := await upload.read_chunk():
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
          raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_UPLOAD_BYTES, actual_size=size
          )
        output.write(chunk)

    async with inference_lock:
      pages = await asyncio.to_thread(_predict, filename)
  except web.HTTPException:
    raise
  except (OSError, ValueError, RuntimeError) as exc:
    logger.exception("OCR request failed")
    raise web.HTTPBadRequest(text="unable to process image") from exc
  finally:
    if filename:
      try:
        os.unlink(filename)
      except OSError:
        logger.warning("cannot remove temporary OCR file: %s", filename)

  text = [value for page in pages for value in _page_text(page)]
  return web.json_response({"result": [{"text": value} for value in text]})


app = web.Application(client_max_size=MAX_UPLOAD_BYTES)
app.router.add_get("/health", health)
app.router.add_post("/api", recognize)

try:
  port = int(os.getenv("PORT", "12345"))
except ValueError as exc:
  raise RuntimeError("PORT must be an integer") from exc

# Containers need a routable listener; override HOST to bind more narrowly.
host = os.getenv("HOST") or socket.gethostbyname(socket.gethostname())
web.run_app(app, host=host, port=port)
