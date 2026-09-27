FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates chromium xvfb xauth libnss3 libatk-bridge2.0-0 libgbm1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN mkdir -p /app/data
EXPOSE 8796
CMD ["xvfb-run", "-a", "-s", "-screen 0 1280x960x24 -nolisten tcp", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8796", "--proxy-headers"]
