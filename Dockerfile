# MISO Copilot backend: FastAPI + poller + RAG, one image.
# CPU-only torch keeps the image a fraction of the CUDA default.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
# torch alone from the official CPU index (its own --index-url, so nothing
# else can be confused off PyPI), then the rest from PyPI as normal
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/

# data/ (snapshots, Chroma, request log) is a volume in any real deployment
RUN useradd --create-home app && mkdir -p data && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
