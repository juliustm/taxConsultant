# Dockerfile
#
# 3.12 rather than the 3.9 this image ran on for its first year. The forcing reason is
# the QR decoder (see requirements.txt): its only wheels that cover both x86_64 and
# arm64 need 3.10 or newer, and without a wheel the install is a CMake build this slim
# image has no toolchain for. 3.9 also stopped receiving security fixes in October
# 2025, and every dependency here is already tested against 3.12 and 3.13.
FROM python:3.12-slim

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