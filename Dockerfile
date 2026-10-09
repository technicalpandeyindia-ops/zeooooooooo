FROM python:3.11-slim

# system deps for playwright chromium
RUN apt-get update && apt-get install -y \
    wget curl ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libxss1 libxtst6 libx11-xcb1 libxcb-dri3-0 \
    fonts-liberation libappindicator3-1 xdg-utils \
    yt-dlp \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .

CMD ["python", "bot.py"]
