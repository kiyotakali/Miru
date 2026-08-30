FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/opt/miru/data \
    LOG_DIR=/opt/miru/logs \
    MIRU_HEADLESS=1 \
    FLASK_DEBUG=false \
    TZ=Asia/Shanghai \
    TIMEZONE=Asia/Shanghai \
    PORT=5001 \
    SERVER_PORT=5001

WORKDIR /opt/miru/app

COPY requirements-server.txt /tmp/requirements-server.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r /tmp/requirements-server.txt

COPY . .

RUN mkdir -p /opt/miru/data /opt/miru/logs \
    && chmod +x scripts/docker_entrypoint.sh

VOLUME ["/opt/miru/data", "/opt/miru/logs"]
EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','5001'), timeout=4).read()" || exit 1

ENTRYPOINT ["scripts/docker_entrypoint.sh"]
CMD ["python", "app.py"]
