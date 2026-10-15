FROM python:3.12-slim

# System deps: tesseract for OCR, node for web UI build
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-vie tesseract-ocr-eng \
    curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY pyproject.toml ./
RUN pip install --no-cache-dir httpx pydantic

# App code
COPY boom.py ./
COPY agent/ ./agent/
COPY gateway/ ./gateway/
COPY acp/ ./acp/
COPY skills/ ./skills/
COPY start.sh stop.sh ./

# Web UI build
COPY apps/web/package.json apps/web/pnpm-lock.yaml ./apps/web/
RUN cd apps/web && npm install --no-audit --no-fund
COPY apps/web/ ./apps/web/
RUN cd apps/web && npm run build

RUN chmod +x start.sh stop.sh

ENV PYTHONPATH=/app:/app/agent/src
ENV BOOM_API_PORT=18080

EXPOSE 18080 5173 8788

CMD ["./start.sh", "all"]
