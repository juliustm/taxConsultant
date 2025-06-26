# Dockerfile
FROM python:3.9-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV LANG C.UTF-8

COPY requirements.txt .
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc build-essential && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y --auto-remove gcc build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 80

CMD ["gunicorn", "wsgi:app", "-b", "0.0.0.0:80", "--worker-class", "gevent", "--timeout", "300"]