FROM python:3.9-slim
RUN pip install kubernetes requests
COPY controller.py /app/controller.py
WORKDIR /app
CMD ["python", "-u", "controller.py"]