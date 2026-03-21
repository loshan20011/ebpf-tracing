FROM python:3.12-slim

# Optimizations for Python in Docker
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir kubernetes requests redis

COPY controller.py .

CMD ["python", "-u", "controller.py"]
