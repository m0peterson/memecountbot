FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY assets ./assets

RUN mkdir -p /app/data
VOLUME ["/app/data"]

ENV DB_PATH=/app/data/memecount.db

CMD ["python", "-m", "bot.main"]
