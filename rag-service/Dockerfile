# The OPT Compass service: API, public page, MCP server and evaluation code in one image.
# The two ONNX models are downloaded at build time, so a container starts without internet access to model hosts.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FASTEMBED_CACHE_PATH=/models

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# The service runs as a non-root user that owns the model cache, so the library can update it without errors.
# It needs a writable home directory: the Hugging Face downloader keeps its logs and cache under ~/.cache, and without one
# the model download failed with "Permission denied" on GitHub Actions.
RUN useradd --system --uid 10001 --create-home appuser && mkdir /models && chown appuser /models
USER appuser

# Bake the embedding and rerank models into the image (the names must match EMBED_MODEL and RERANK_MODEL defaults).
RUN python -c "from fastembed import TextEmbedding; from fastembed.rerank.cross_encoder import TextCrossEncoder; \
TextEmbedding('BAAI/bge-base-en-v1.5'); TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2')"

COPY app ./app
COPY eval ./eval
COPY snapshot ./snapshot
COPY --chmod=755 docker-entrypoint.sh ./

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

ENTRYPOINT ["./docker-entrypoint.sh"]
