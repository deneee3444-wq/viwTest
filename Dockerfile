FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PORT=10000
ENV DISPLAY=:99
ENV PYTHONUNBUFFERED=1

# Xvfb ve xauth kurulumu
RUN apt-get update && apt-get install -y --no-install-recommends xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir flask gunicorn playwright requests \
    && playwright install --with-deps chromium

COPY app.py .

EXPOSE 10000

CMD ["sh", "-c", "Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset & exec gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4 --timeout 300 app:app"]
