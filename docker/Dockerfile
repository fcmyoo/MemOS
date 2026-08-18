# Base image
FROM python:3.11-slim

# Install dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    build-essential \
    libffi-dev \
    python3-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Set Hugging Face mirror
ENV HF_ENDPOINT=https://hf-mirror.com

# Install Python packages
COPY docker/requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Copy pytest configuration (asyncio_mode=auto etc.) so in-container test runs
# behave identically to the host/CI environment.
COPY pyproject.toml .

# Copy application code
COPY docker/ ./docker/
COPY src/ ./src/

# Set Python import path
ENV PYTHONPATH=/app/src

# Expose port
EXPOSE 8000

# Run as non-root for production safety.
# The source tree is mounted read-write for dev (compose volumes); the app
# itself only needs read access, and the uvicorn worker runs as the app user.
RUN useradd --create-home --uid 1000 memos \
    && chown -R memos:memos /app
USER memos

# Start the server without --reload (production). Dev reload is enabled by
# overriding the command in docker-compose or with `docker compose run`.
CMD ["uvicorn", "memos.api.server_api:app", "--host", "0.0.0.0", "--port", "8000"]
