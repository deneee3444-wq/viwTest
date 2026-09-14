FROM python:3.12-slim-bookworm

WORKDIR /app

# Sistem gereksinimleri ve Playwright Chromium kurulumu
RUN pip install --no-cache-dir flask gunicorn playwright requests \
    && playwright install --with-deps chromium

COPY app.py .

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4 --timeout 300 app:app"]
