FROM python:3.12-slim-bookworm

WORKDIR /app

# Xvfb sanal ekran yöneticisi kurulumu
RUN apt-get update && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir flask gunicorn playwright requests \
    && playwright install --with-deps chromium

COPY app.py .

CMD ["sh", "-c", "exec xvfb-run --auto-servernum --server-args='-screen 0 1920x1080x24' gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4 --timeout 300 app:app"]
