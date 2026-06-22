FROM python:3.11-slim
# cache bust 2206

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Dependências do sistema pro Playwright/Chromium rodar headless
# Lista oficial: https://playwright.dev/python/docs/browsers#install-system-dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl fonts-liberation fonts-noto-color-emoji \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    libxshmfence1 libx11-6 libx11-xcb1 libxcb1 libxext6 \
    libdbus-1-3 libexpat1 libglib2.0-0 libgcc-s1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Baixa o Chromium do Playwright explicitamente (com deps)
RUN python -m playwright install --with-deps chromium

COPY . .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:8000/health').read()" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
