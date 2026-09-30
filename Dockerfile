FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install chromium

COPY autotik/ ./autotik/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    TZ=Africa/Kigali \
    DATA_DIR=/app/data \
    COOKIE_DIR=/app/cookies

ENTRYPOINT ["python", "-m", "autotik.cli"]
