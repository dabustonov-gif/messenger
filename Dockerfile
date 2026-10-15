# Образ с GDAL (нужен для карт). Мессенджер работает и без него — карты тогда отключаются.
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.9.3
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt
COPY app ./app
COPY gis ./gis
COPY manage.py .
ENV MSG_DATA_DIR=/data
VOLUME /data
USER 1000:1000
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--ws-ping-interval", "25"]
