FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

# Install minimal system deps needed by Python packages
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /tmp/pyproject.toml
RUN pip install --no-cache-dir pip-tools
RUN python -m piptools compile /tmp/pyproject.toml -o /tmp/requirements.txt && pip install -r /tmp/requirements.txt


CMD ["/bin/sh", "-c", "echo carqna-dev ready && sleep infinity"]
