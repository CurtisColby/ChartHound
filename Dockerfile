FROM python:3.12-slim

LABEL maintainer="ChartHound"
LABEL description="ChartHound — Dockerized Music Metadata Engine"

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    flac \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ ./app/

# Copy frontend
COPY frontend/ ./frontend/

# Data directory (mounted as volume in production)
RUN mkdir -p /data

# Expose internal port (mapped to 8585 in docker-compose)
EXPOSE 8000

# Launch with Uvicorn — 2 workers for a single-user home server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
