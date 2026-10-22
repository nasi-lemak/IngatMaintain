FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/ingatmaintain.sqlite3

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ingatmaintain ./ingatmaintain
COPY run.py .

RUN useradd --system --uid 1000 app && mkdir -p /data /app/instance && chown -R app /data /app/instance
USER app
VOLUME ["/data"]
EXPOSE 8000
CMD ["python", "run.py"]
