FROM python:3.12-slim

WORKDIR /app

RUN pip install flask flask-cors kubernetes requests redis

COPY app.py .

CMD ["python", "-u", "app.py"]
