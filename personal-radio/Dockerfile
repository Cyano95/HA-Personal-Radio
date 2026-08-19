FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --pre \
    "yt-dlp[default]" \
    fastapi \
    "uvicorn[standard]" \
    httpx \
    aiofiles \
    numpy

COPY backend/              /app/backend/
COPY frontend/             /app/frontend/
COPY panel_resource/       /app/panel_resource/
COPY companion_integration/ /app/companion_integration/
COPY run.sh                /run.sh

RUN sed -i 's/\r//' /run.sh && chmod +x /run.sh

WORKDIR /app
CMD ["/run.sh"]
