FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create required directories
RUN mkdir -p logs reports data

# Health check via log file existence and recency (updated within last 10 minutes)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD test -f logs/main.log && test $(find logs/main.log -mmin -10 | wc -l) -gt 0 || exit 1

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

CMD ["python", "main.py"]
