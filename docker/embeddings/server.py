"""Internal-only CPU embedding service; never publish this port to the Internet."""

import asyncio
import os

from aiohttp import web  # type: ignore[import-not-found]

from luoxu.semantic import (  # type: ignore[import-not-found]
  DIMENSIONS, MAX_DOCUMENT_CHARS, MODEL_ID, MODEL_NAME, MODEL_REVISION, STRIP_CHARS,
)

QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


def create_app(encode):
  app = web.Application(client_max_size=2 * 1024 * 1024)
  lock = asyncio.Lock()

  async def health(_request):
    return web.json_response({"model": MODEL_ID, "dimensions": DIMENSIONS})

  async def embed(request):
    try:
      data = await request.json()
    except ValueError as exc:
      raise web.HTTPBadRequest(text="invalid JSON") from exc
    if not isinstance(data, dict):
      raise web.HTTPBadRequest(text="JSON object required")
    texts = data.get("texts")
    query = data.get("query", False)
    if (
      type(query) is not bool
      or not isinstance(texts, list)
      or not 1 <= len(texts) <= 32
      or any(not isinstance(t, str) or not t.strip(STRIP_CHARS) or len(t) > MAX_DOCUMENT_CHARS for t in texts)
    ):
      raise web.HTTPBadRequest(text="expected 1-32 nonempty texts of at most 8192 characters")
    if lock.locked():
      raise web.HTTPServiceUnavailable(text="embedding service busy", headers={"Retry-After": "1"})
    async with lock:
      inputs = [QUERY_PREFIX + text if query else text for text in texts]
      task = asyncio.create_task(asyncio.to_thread(encode, inputs))
      try:
        vectors = await asyncio.shield(task)
      except asyncio.CancelledError:
        # Cancelling an HTTP request cannot stop the CPU thread. Keep the lock
        # until it completes so a new request cannot run a second inference.
        await task
        raise
    return web.json_response({"model": MODEL_ID, "vectors": vectors})

  app.router.add_get("/health", health)
  app.router.add_post("/embed", embed)
  return app


def load_encoder():
  # Heavy dependencies live only in this optional service, not the Luoxu image.
  import torch  # type: ignore[import-not-found]
  from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

  try:
    threads = int(os.environ.get("EMBEDDING_THREADS", "4"))
    if not 1 <= threads <= 16:
      raise ValueError
  except ValueError as exc:
    raise ValueError("EMBEDDING_THREADS must be between 1 and 16") from exc
  torch.set_num_threads(threads)
  model = SentenceTransformer(
    MODEL_NAME, revision=MODEL_REVISION, device="cpu", trust_remote_code=False,
  )
  model.max_seq_length = 512

  def encode(texts):
    return model.encode(
      texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False,
    ).tolist()

  return encode


if __name__ == "__main__":
  # Download/load before listening; health means the model is ready to serve.
  # Direct local runs bind loopback. The Docker image explicitly opts in to an
  # internal network listener; Compose deliberately has no host ports mapping.
  web.run_app(
    create_app(load_encoder()), host=os.environ.get("EMBEDDING_HOST", "127.0.0.1"),
    port=8080, access_log=None,
  )
