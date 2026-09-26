FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models \
    EMBEDDING_THREADS=4 \
    TOKENIZERS_PARALLELISM=false
WORKDIR /app
# CPU wheels only; no CUDA libraries in this optional image.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url https://download.pytorch.org/whl/cpu torch==2.6.0 \
 && pip install sentence-transformers==3.4.1 aiohttp==3.12.15
ENV EMBEDDING_HOST=0.0.0.0
COPY luoxu/__init__.py luoxu/semantic.py luoxu/storage.py ./luoxu/
COPY docker/embeddings/server.py ./server.py
VOLUME ["/models"]
EXPOSE 8080
CMD ["python", "server.py"]
