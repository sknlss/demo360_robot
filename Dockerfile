FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY funnels.yml .

# база, замок и кеш фото лежат в /app/data — подключите том
CMD ["python", "-m", "app"]
