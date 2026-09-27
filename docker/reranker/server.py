"""Internal-only CPU reranker. Loading finishes before the health listener starts."""

import asyncio
import os

from aiohttp import web

from luoxu.rerank import MODEL_ID, MODEL_NAME, MODEL_REVISION, MAX_BATCH, valid_score
from luoxu.semantic import MAX_DOCUMENT_CHARS, MAX_QUERY_CHARS, STRIP_CHARS


def create_app(predict):
  app = web.Application(client_max_size=2 * 1024 * 1024)
  lock = asyncio.Lock()
  tasks = set()

  async def health(_request):
    return web.json_response({"model": MODEL_ID})

  def finished(task):
    tasks.discard(task)
    # Consume exceptions even if the HTTP request was cancelled. The CPU thread
    # owns admission until completion, including repeated request cancellation.
    if not task.cancelled():
      task.exception()
    lock.release()

  async def rerank(request):
    try:
      data = await request.json()
    except ValueError as exc:
      raise web.HTTPBadRequest(text="invalid JSON") from exc
    if not isinstance(data, dict):
      raise web.HTTPBadRequest(text="JSON object required")
    query, texts = data.get("query"), data.get("texts")
    if (not isinstance(query, str) or not query.strip(STRIP_CHARS) or len(query) > MAX_QUERY_CHARS
        or not isinstance(texts, list) or not 1 <= len(texts) <= MAX_BATCH
        or any(not isinstance(t, str) or not t.strip(STRIP_CHARS) or len(t) > MAX_DOCUMENT_CHARS for t in texts)):
      raise web.HTTPBadRequest(text="expected query (1-2000 characters) and 1-32 texts (1-8192 characters)")
    if lock.locked():
      raise web.HTTPServiceUnavailable(text="reranking service busy", headers={"Retry-After": "1"})
    await lock.acquire()
    pairs = [(query.strip(STRIP_CHARS), t.strip(STRIP_CHARS)) for t in texts]
    task = asyncio.create_task(asyncio.to_thread(predict, pairs))
    tasks.add(task)
    task.add_done_callback(finished)
    try:
      scores = await asyncio.shield(task)
      if not isinstance(scores, list) or len(scores) != len(texts) or any(not valid_score(s) for s in scores):
        raise ValueError("invalid model output")
    except Exception as exc:
      raise web.HTTPServiceUnavailable(text="reranking inference failed") from exc
    return web.json_response({"model": MODEL_ID, "scores": [
      {"index": index, "score": score} for index, score in enumerate(scores)
    ]})

  async def cleanup(_app):
    if tasks:
      await asyncio.gather(*tasks, return_exceptions=True)

  app.on_cleanup.append(cleanup)
  app.router.add_get("/health", health)
  app.router.add_post("/rerank", rerank)
  return app


def load_predictor():
  # These dependencies are installed only in the optional CPU service image.
  import torch  # type: ignore[import-not-found]
  from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]

  try:
    threads = int(os.environ.get("RERANKER_THREADS", "4"))
    batch_size = int(os.environ.get("RERANKER_BATCH_SIZE", "8"))
    if not 1 <= threads <= 16 or not 1 <= batch_size <= 32:
      raise ValueError
  except ValueError as exc:
    raise ValueError("RERANKER_THREADS must be 1-16 and RERANKER_BATCH_SIZE 1-32") from exc
  torch.set_num_threads(threads)
  torch.set_num_interop_threads(1)
  model = CrossEncoder(
    MODEL_NAME, revision=MODEL_REVISION, device="cpu", max_length=512,
    trust_remote_code=False, automodel_args={"use_safetensors": True},
    default_activation_function=torch.nn.Identity(),
  )
  model.model.eval()

  def predict(pairs):
    with torch.inference_mode():
      # Override ST's default single-label sigmoid: apply it exactly once here.
      logits = model.predict(
        pairs, activation_fct=torch.nn.Identity(), batch_size=batch_size,
        show_progress_bar=False, convert_to_tensor=True,
      )
      return torch.sigmoid(logits).reshape(-1).tolist()

  return predict


if __name__ == "__main__":
  web.run_app(
    create_app(load_predictor()), host=os.environ.get("RERANKER_HOST", "127.0.0.1"),
    port=8080, access_log=None,
  )
