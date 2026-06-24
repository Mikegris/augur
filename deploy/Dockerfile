# AJTA — AUGUR app + trading agent image (AJTA-SPEC-1.0 §22.1).
# Bare-metal (./run.sh) remains the simplest install; this image is the
# optional containerized deployment for the local-only profile.
FROM python:3.9-slim

WORKDIR /app

# system deps for scientific stack + lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# app source (DB lives on a mounted volume at AUGUR_DB_PATH, never in the image)
COPY . .

ENV PORT=5001 \
    AUGUR_DB_PATH=/data/wealth.db \
    AUGUR_NO_SELF_UPDATE=1
EXPOSE 5001

# default: run the web app (paper-first; all trade switches false).
CMD ["python", "app.py"]
