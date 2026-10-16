FROM python:3.12-slim

# System deps: tesseract for OCR, curl for the healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-vie tesseract-ocr-eng \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (must match pyproject.toml [project] dependencies)
COPY pyproject.toml ./
RUN pip install --no-cache-dir httpx "pydantic>=2" pyyaml

# App code
COPY boom.py ./
COPY agent/ ./agent/
COPY gateway/ ./gateway/
COPY acp/ ./acp/
COPY skills/ ./skills/
COPY start.sh stop.sh ./

RUN chmod +x start.sh stop.sh

ENV PYTHONPATH=/app:/app/agent/src
ENV BOOM_API_PORT=18080

EXPOSE 18080 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:18080/api/health || exit 1

CMD ["./start.sh", "all"]
