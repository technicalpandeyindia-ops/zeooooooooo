FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PORT=10000
ENV PIP_ROOT_USER_ACTION=ignore

# Install system dependencies required by Playwright Chromium and FFmpeg
RUN apt-get update && apt-get install -y \
    wget curl ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libxss1 libxtst6 libx11-xcb1 libxcb-dri3-0 \
    fonts-liberation xdg-utils ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY . .

EXPOSE 10000

CMD ["python", "bot.py"]
