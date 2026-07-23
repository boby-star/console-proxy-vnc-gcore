FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY console_proxy ./console_proxy
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 appuser

USER appuser

EXPOSE 5000

CMD ["python", "-m", "console_proxy"]