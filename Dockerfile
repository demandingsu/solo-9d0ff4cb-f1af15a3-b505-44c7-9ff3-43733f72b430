FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RECALLQC_DATA_DIR=/data

WORKDIR /srv
COPY app /srv/app
COPY example_site /srv/example_site

RUN useradd --uid 10001 --create-home recall \
 && mkdir -p /data /srv \
 && chown -R recall:recall /data /srv
USER recall

EXPOSE 8000 8080
CMD ["python", "-m", "app.server"]
