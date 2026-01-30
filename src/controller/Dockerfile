FROM python:3.12-slim

WORKDIR /app

RUN pip install kubernetes requests

COPY controller.py .

CMD ["python", "-u", "controller.py"]