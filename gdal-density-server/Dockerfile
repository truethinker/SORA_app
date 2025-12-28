FROM ghcr.io/osgeo/gdal:ubuntu-small-3.8.0

WORKDIR /app

# Python runtime + common utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY server.py .

# Optional: bake small test data in the image by copying /data.
# For large .tif files, prefer downloading at runtime from object storage (see README).
# COPY data/ ./data/

ENV PYTHONUNBUFFERED=1
ENV PORT=8080

EXPOSE 8080

# Railway sets $PORT at runtime; fall back to 8080 locally.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 2 --threads 4 --timeout 180 server:app"]
