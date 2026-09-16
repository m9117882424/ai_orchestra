FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ARG RUNNER_UID=10001
ARG RUNNER_GID=10001

RUN groupadd --gid "${RUNNER_GID}" orchestra \
    && useradd --uid "${RUNNER_UID}" --gid orchestra --home-dir /home/orchestra \
       --create-home --shell /usr/sbin/nologin orchestra \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates git jq make ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && chmod 0755 /home/orchestra

COPY entrypoint.py /opt/ai-orchestra-runner/entrypoint.py
RUN chmod 0555 /opt/ai-orchestra-runner/entrypoint.py

WORKDIR /workspace
USER orchestra

ENV HOME=/home/orchestra \
    CI=1 \
    NO_COLOR=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python3", "/opt/ai-orchestra-runner/entrypoint.py"]
CMD ["python3", "--version"]
