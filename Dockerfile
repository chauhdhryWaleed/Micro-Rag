FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer-cached; only rebuilds when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY pipeline/ ./pipeline/
COPY rag/ ./rag/
COPY static/ ./static/
COPY logging_config.py .

COPY dataset/ ./dataset/

EXPOSE 8000

CMD ["uvicorn", "rag.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
