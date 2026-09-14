FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PORT=10000

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        xvfb \
        xauth \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
        "Flask>=3.0,<4" \
        "gunicorn>=22,<24" \
        "playwright>=1.49,<2" \
        "requests>=2.32,<3" \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --create-home appuser \
    && chown appuser:appuser /app

COPY --chown=appuser:appuser app.py /app/app.py

USER appuser

EXPOSE 10000

ENTRYPOINT ["/usr/bin/tini", "--"]

# Ekran boyutu ilk kodundaki gibi 1920x1080.
# Görüntü ve işlem durumu bellekte olduğu için workers=1 korunmalıdır.
CMD ["sh", "-c", "exec xvfb-run -a -s '-screen 0 1920x1080x24 -nolisten tcp' gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --worker-class gthread --workers 1 --threads 8 --timeout 420 --graceful-timeout 30 --keep-alive 5 --access-logfile - --error-logfile - --capture-output"]
