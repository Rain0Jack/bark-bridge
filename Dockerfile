FROM python:3.12-alpine

LABEL org.opencontainers.image.title="bark-bridge" \
      org.opencontainers.image.description="Forward ntfy messages to Bark push notifications (multi-device, multi-user, WebUI)" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
COPY bark-bridge.py /app/bark-bridge.py
COPY webui.html /app/webui.html

ENV DATA_DIR=/app/data \
    PORT=8080 \
    PYTHONUNBUFFERED=1

# SQLite 数据库目录，compose 里请挂载持久化卷
RUN mkdir -p /app/data
VOLUME ["/app/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/api/health',timeout=3)" || exit 1

# 零第三方依赖，直接跑脚本
CMD ["python3", "/app/bark-bridge.py"]
