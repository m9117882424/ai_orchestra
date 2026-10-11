FROM node:22-bookworm-slim AS codex-cli

RUN npm install --global --ignore-scripts @openai/codex@0.156.1

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/home/orchestra CODEX_HOME=/home/orchestra/.codex

RUN groupadd --gid 10001 orchestra \
    && useradd --uid 10001 --gid 10001 --home-dir /home/orchestra --create-home orchestra \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o 10001 -g 10001 -m 0700 /home/orchestra/.codex /private/codex-results \
    && chmod 0700 /home/orchestra \
    && install -d -m 0555 /workspace/worktrees/managed /run/codex-subscription-private /app

COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-cli /usr/local/lib/node_modules/@openai/codex /usr/local/lib/node_modules/@openai/codex
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex

WORKDIR /app
COPY control_plane/requirements.lock /app/requirements.lock
COPY control_plane/app/__init__.py control_plane/app/workspace_protocol.py /app/control_plane/app/
COPY scripts/codex_task.py scripts/codex_execution_worker.py /app/scripts/
COPY deploy/codex-worker/preflight.py /app/preflight.py
COPY deploy/codex-worker/start.sh /app/start.sh
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.lock \
    && chmod 0555 /app/start.sh /app/preflight.py \
    && chmod -R a+rX /app

USER 10001:10001
ENTRYPOINT ["/app/start.sh"]
