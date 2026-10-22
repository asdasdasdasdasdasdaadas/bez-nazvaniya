FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/bot.db

WORKDIR /app

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin bot \
    && mkdir -p /data \
    && chown bot:bot /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

USER bot

CMD ["python", "-m", "bot"]
