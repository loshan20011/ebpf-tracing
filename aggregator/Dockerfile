FROM python:3.9-slim

WORKDIR /app

RUN pip install flask flask-cors kubernetes requests

COPY app.py .

CMD ["python", "-u", "app.py"]
