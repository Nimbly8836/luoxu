"""Optional CPU cross-encoder protocol; no ML imports in the application."""

import asyncio
import json
import math
from functools import partial

import aiohttp

from .semantic import MAX_DOCUMENT_CHARS, MAX_QUERY_CHARS, STRIP_CHARS, SemanticUnavailable

MODEL_NAME = "BAAI/bge-reranker-base"
MODEL_REVISION = "2cfc18c9415c912f9d8155881c133215df768a70"
MODEL_ID = f"{MODEL_NAME}@{MODEL_REVISION}:strip-pair-512-sigmoid-v1"
MAX_BATCH = 32
REQUEST_TIMEOUT = 60


def valid_score(value):
  return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


class RerankerClient:
  def __init__(self, config):
    if not isinstance(config, dict):
      raise ValueError("database.semantic.rerank must be a table")
    self.enabled = config.get("enabled", False)
    if type(self.enabled) is not bool:
      raise ValueError("database.semantic.rerank.enabled must be boolean")
    self.endpoint = config.get("endpoint", "http://reranker:8080/rerank")
    if not isinstance(self.endpoint, str) or not self.endpoint.startswith(("http://", "https://")):
      raise ValueError("database.semantic.rerank.endpoint must be an HTTP(S) URL")
    self.candidates = config.get("candidates", 50)
    self.page_size = config.get("page_size", 20)
    self.min_score = config.get("min_score", 0.5)
    for name, value, maximum in (("candidates", self.candidates, 200), ("page_size", self.page_size, 50)):
      if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"database.semantic.rerank.{name} must be an integer in 1-{maximum}")
    if not valid_score(self.min_score):
      raise ValueError("database.semantic.rerank.min_score must be a finite number in 0-1")
    self.session = None
    self.slots = asyncio.Semaphore(2)

  async def close(self):
    if self.session is not None:
      await self.session.close()
      self.session = None

  async def rerank(self, query, texts):
    query = query.strip(STRIP_CHARS)
    if not query or len(query) > MAX_QUERY_CHARS or len(texts) > 200:
      raise ValueError("invalid reranking input")
    scores = []
    try:
      # Includes admission wait and ALL batches, not a fresh deadline per batch.
      async with asyncio.timeout(REQUEST_TIMEOUT), self.slots:
        if self.session is None:
          self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            json_serialize=partial(json.dumps, ensure_ascii=False),
          )
        for offset in range(0, len(texts), MAX_BATCH):
          batch = [t.strip(STRIP_CHARS)[:MAX_DOCUMENT_CHARS] for t in texts[offset:offset + MAX_BATCH]]
          async with self.session.post(
            self.endpoint, json={"query": query, "texts": batch}, allow_redirects=False,
          ) as response:
            if response.status != 200:
              raise SemanticUnavailable("reranking service unavailable")
            # Bound untrusted response memory before decoding.
            try:
              body = await response.content.readexactly(65537)
            except asyncio.IncompleteReadError as exc:
              body = exc.partial
            if len(body) > 65536:
              raise SemanticUnavailable("invalid reranking response size")
            data = json.loads(body)
          if not isinstance(data, dict) or data.get("model") != MODEL_ID:
            raise SemanticUnavailable("reranking model mismatch")
          results = data.get("scores")
          if not isinstance(results, list) or len(results) != len(batch):
            raise SemanticUnavailable("invalid reranking count")
          for index, result in enumerate(results):
            if (not isinstance(result, dict) or type(result.get("index")) is not int
                or result["index"] != index or not valid_score(result.get("score"))):
              raise SemanticUnavailable("invalid reranking score/order")
            scores.append(result["score"])
      return scores
    except (aiohttp.ClientError, TimeoutError, ValueError, OverflowError) as exc:
      raise SemanticUnavailable("reranking service unavailable") from exc
