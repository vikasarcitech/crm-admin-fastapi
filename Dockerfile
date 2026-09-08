# Slim runtime image for Fargate. Multi-stage keeps build tooling out of
# the final layer; asyncpg and bcrypt ship wheels, so no compiler needed.
FROM python:3.12-slim AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
WORKDIR /app

# postgresql-client: pg_dump, for the database backups in
# app/routers/ops.py. Without it, backups fail with a clear message
# rather than silently doing nothing.
RUN apt-get update \
 && apt-get install --no-install-recommends -y postgresql-client \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /install /usr/local
COPY app ./app
COPY db ./db
COPY public ./public

# Local media storage, when MEDIA_STORAGE=local. On Fargate this layer
# is ephemeral — use MEDIA_STORAGE=s3 in production or uploads vanish
# with the task.
RUN mkdir -p /app/var/media

# Run unprivileged: a container escape shouldn't start from root.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000

# RUN_WORKERS=0 here and a separate single-task service for the worker,
# so N replicas don't all poll the webhook queue.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
