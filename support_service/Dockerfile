# syntax=docker/dockerfile:1
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/srv/cli
WORKDIR /srv
COPY support_service/requirements.lock /tmp/requirements.lock
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then PIP_CERT=/run/secrets/proxy_ca pip install --no-cache-dir -r /tmp/requirements.lock; else pip install --no-cache-dir -r /tmp/requirements.lock; fi
COPY --chmod=755 cli/lvct ./cli/lvct
COPY --chmod=755 support_service ./support_service
RUN useradd --uid 10001 --no-create-home support
USER 10001:10001
EXPOSE 19473
CMD ["uvicorn", "support_service.app:app", "--host", "0.0.0.0", "--port", "19473", "--no-proxy-headers", "--no-access-log", "--limit-concurrency", "32", "--timeout-keep-alive", "5"]
