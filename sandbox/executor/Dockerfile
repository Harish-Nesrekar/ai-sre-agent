FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir fastapi uvicorn prometheus-client httpx

RUN useradd --uid 10001 --create-home sandbox \
    && mkdir -p /workspace/tests /workspace/apps/demo /var/log/sre \
    && chown -R sandbox:sandbox /workspace /var/log/sre

USER 10001:10001

WORKDIR /workspace

CMD ["python", "-c", "pass"]