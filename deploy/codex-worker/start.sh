#!/bin/sh
set -eu
python /app/preflight.py
exec python -m scripts.codex_execution_worker \
  --broker http://control-plane:8080 \
  --isolated-control-network \
  --token-file /run/codex-subscription-private/one-shot.token \
  --output /private/codex-results
