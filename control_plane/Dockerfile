FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system orchestra \
    && useradd --system --gid orchestra --home-dir /app orchestra

WORKDIR /app

COPY requirements.in requirements-dev.in requirements.lock requirements-dev.lock dependency-locks.sha256 ./
RUN sha256sum --check dependency-locks.sha256 \
    && pip install --no-cache-dir --require-hashes --requirement requirements.lock \
    && sha256sum requirements.lock > runtime-lock.sha256

COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY app ./app

USER orchestra

EXPOSE 8080

CMD ["uvicorn", "app.production:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips=*"]
