FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

# Reguläre Abhängigkeiten: NUR stabile Versionen.
# (--pre darf hier NIE stehen — es würde z.B. httpx als 1.0-dev-Build
#  installieren, dessen API inkompatibel ist.)
RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    httpx \
    aiofiles \
    numpy \
    "yt-dlp[default]"

# Nur den yt-dlp-KERN auf den Nightly-Kanal anheben. yt-dlp selbst hat
# keine Pflicht-Abhängigkeiten — dieser Schritt kann kein anderes Paket
# anfassen.
RUN pip install --no-cache-dir --upgrade --pre yt-dlp

COPY backend/              /app/backend/
COPY frontend/             /app/frontend/
COPY panel_resource/       /app/panel_resource/
COPY companion_integration/ /app/companion_integration/
COPY run.sh                /run.sh

RUN sed -i 's/\r//' /run.sh && chmod +x /run.sh

WORKDIR /app
CMD ["/run.sh"]
