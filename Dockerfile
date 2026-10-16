FROM python:3.11-slim-trixie
WORKDIR /app
# Only the Docker client is needed; the daemon stays on the host.
RUN apt-get update && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 dashboard && useradd -u 10001 -g dashboard -M dashboard
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY app.py ./
COPY frontend ./frontend
RUN mkdir /app/data && chown dashboard:dashboard /app/data
USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/app/data
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=3)"
# One worker is intentional: sampler, sessions and rate limits are process-local.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--worker-class", "gthread", "--threads", "16", "--timeout", "90", "--worker-tmp-dir", "/tmp", "--no-control-socket", "app:create_app()"]
