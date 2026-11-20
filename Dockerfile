FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY late_stream ./late_stream
RUN pip install --no-cache-dir .

ENV LATE_STREAM_DATA=/data LATE_STREAM_PORT=8080 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
# SIGTERM lets running streams stop cleanly; they are marked "interrupted"
# and auto-resume if the container is back within 5 minutes.
STOPSIGNAL SIGTERM
CMD ["late-stream", "serve"]
