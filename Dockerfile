# Ingestion worker image (used by Railway / any container host).
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ingest/ ./ingest/
COPY db/ ./db/
COPY notify/ ./notify/
COPY feedback/ ./feedback/

# Default command is a no-op help; each Railway service overrides it:
#   python -m ingest.run sales     (manual)
#   python -m ingest.daily         (railway.json,       cron 05:30 UTC)
#   python -m notify.brief         (railway.brief.json, cron 10:00 UTC)
#   gunicorn feedback.app:app      (railway.feedback.json, always-on survey site)
#   python -m feedback.send        (guest survey emails; DRY RUN unless --live and FEEDBACK_SEND_ENABLED=true)
CMD ["python", "-m", "ingest.run", "--help"]
