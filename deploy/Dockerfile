# Curio-Cabinet container image (for adopters not on NearlyFreeSpeech).
FROM python:3.12-slim

# libheif enables the optional HEIC upload support
RUN apt-get update \
    && apt-get install -y --no-install-recommends libheif1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir ".[heic]" gunicorn

# The instance (config, db, images, .env) is mounted at runtime.
ENV CABINET_INSTANCE=/instance
EXPOSE 8099

# One worker (SQLite single-writer); scale with threads.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--worker-class", "gthread", \
     "--bind", "0.0.0.0:8099", "curio_cabinet.app:create_app()"]
