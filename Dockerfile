# Ingestion worker image (used by Railway / any container host).
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ingest/ ./ingest/
COPY db/ ./db/
COPY notify/ ./notify/

# Default command is a no-op help; each Railway service overrides it:
#   python -m ingest.run sales     (manual)
#   python -m ingest.daily         (railway.json,       cron 05:30 UTC)
#   python -m notify.brief         (railway.brief.json, cron 10:00 UTC)
CMD ["python", "-m", "ingest.run", "--help"]
