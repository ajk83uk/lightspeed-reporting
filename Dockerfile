# Ingestion worker image (used by Railway / any container host).
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ingest/ ./ingest/
COPY db/ ./db/

# Default command is a no-op help; the scheduler overrides it, e.g.
#   python -m ingest.run sales
#   python -m ingest.run items
CMD ["python", "-m", "ingest.run", "--help"]
