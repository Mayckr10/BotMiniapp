# Imagem para Railway, Fly.io, Render ou qualquer VPS com Docker
FROM python:3.12-slim

# FFmpeg gera a amostra embaçada dos vídeos (sem ele, vídeos ganham uma capa genérica)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# O banco fica num volume persistente montado em /data
ENV DB_PATH=/data/bot.db \
    WEBHOOK_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1
EXPOSE 8080

# Railway/Render informam a porta em $PORT; nos outros casos usa 8080
CMD ["sh", "-c", "mkdir -p /data && WEBHOOK_PORT=${PORT:-${WEBHOOK_PORT:-8080}} exec python main.py"]
