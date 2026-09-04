FROM python:3.12-slim

ARG LITELLM_VERSION=1.98.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system router \
    && useradd --system --gid router --home-dir /app router \
    && pip install --no-cache-dir "litellm[proxy]==${LITELLM_VERSION}"

WORKDIR /app
USER router

EXPOSE 4000

ENTRYPOINT ["litellm"]
CMD ["--config", "/app/config.yaml", "--port", "4000"]
