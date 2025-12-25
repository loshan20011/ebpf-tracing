FROM python:3.9-slim

WORKDIR /app

# Install system dependencies (optional, but good for safety)
RUN pip install --no-cache-dir --upgrade pip

# Install Python Libraries
# ✅ ADD FLASK HERE
RUN pip install --no-cache-dir kubernetes flask requests

# Copy the brain code
COPY controller.py .

# Run the brain
CMD ["python", "-u", "controller.py"]