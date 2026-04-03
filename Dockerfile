FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

RUN chown -R appuser:appuser /app

USER appuser


FROM base AS web

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "src.web.wsgi:app"]


FROM base AS bot

CMD ["python", "-m", "src.main", "bot"]


FROM base AS worker

CMD ["python", "-m", "src.main", "daily-report-worker"]
