FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Ensure the database directory exists and has correct permissions
RUN mkdir -p /app/data && chown -R 1000:1000 /app

# Change DATABASE_URL to use a persistent volume
ENV DATABASE_URL=sqlite:////app/data/db.sqlite3
ENV WEBHOOK_HOST=0.0.0.0
ENV WEBHOOK_PORT=8080

EXPOSE 8080

CMD ["python", "-m", "app.main"]
