FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Reuse the project's checked-in, hash-pinned runtime dependency lock.
COPY control_plane/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.lock

COPY notifications/__init__.py /app/notifications/__init__.py
COPY notifications/telegram_notifier.py /app/notifications/telegram_notifier.py

# The dedicated worker has no shell or API credentials. It runs as container
# root only because the user-provided bind-mounted secret files are root:0600;
# it receives no elevated Linux capabilities or host filesystem mounts.
CMD ["python", "-m", "notifications.telegram_notifier"]
