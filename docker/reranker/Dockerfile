FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models \
    RERANKER_THREADS=4 \
    RERANKER_BATCH_SIZE=8 \
    TOKENIZERS_PARALLELISM=false
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url https://download.pytorch.org/whl/cpu torch==2.6.0 \
 && pip install sentence-transformers==3.4.1 aiohttp==3.12.15
ENV RERANKER_HOST=0.0.0.0
COPY luoxu/__init__.py luoxu/semantic.py luoxu/rerank.py luoxu/storage.py ./luoxu/
COPY docker/reranker/server.py ./server.py
VOLUME ["/models"]
EXPOSE 8080
CMD ["python", "server.py"]
