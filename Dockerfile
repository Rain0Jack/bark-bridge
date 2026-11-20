FROM python:3.12-alpine

LABEL org.opencontainers.image.title="bark-bridge" \
      org.opencontainers.image.description="Forward ntfy messages to Bark push notifications" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
COPY bark-bridge.py /app/bark-bridge.py

# 无第三方依赖，直接跑脚本
CMD ["python3", "/app/bark-bridge.py"]
