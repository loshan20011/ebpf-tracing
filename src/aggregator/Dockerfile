FROM python:3.12-slim

# Prevent python from writing pyc files or buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies
# 'requests' handles HTTP, 'redis' handles DB, 'kubernetes' handles discovery
RUN pip install --no-cache-dir flask flask-cors kubernetes requests redis

COPY *.py ./

# Run unbuffered (-u) to see logs in Kubernetes immediately
CMD ["python", "-u", "aggregator.py"]
