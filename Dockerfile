FROM python:3.13-slim

WORKDIR /app

# LibreOffice for Office → PDF conversion
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-core \
    libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/
COPY src/ /app/src/
COPY run.py /app/

RUN pip install --no-cache-dir . gunicorn

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/login')" || exit 1

EXPOSE 5000

CMD ["gunicorn", "--worker-class", "gthread", "--threads", "8", "--bind", "0.0.0.0:5000", "--timeout", "120", "run:app"]
