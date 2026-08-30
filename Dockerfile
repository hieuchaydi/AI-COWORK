# Base image with Python 3.13 on Debian Bookworm (Playwright supported)
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Upgrade pip and install uv
RUN pip install --no-cache-dir --upgrade pip uv

# Copy project files
COPY . .

# Setup Python virtual environment and dependencies
RUN uv venv .venv \
    && .venv/bin/pip install -e "connect-ai[messaging,browser,bedrock,dev]" \
    && .venv/bin/playwright install chromium --with-deps

# Setup GUI dependencies
RUN cd connect-ai/surfaces/gui && npm install

# Expose required ports
# 1420: GUI, 8765: backend runtime, 8766: helper
EXPOSE 1420 8765 8766

# Ensure required directories exist for runtime
RUN mkdir -p logs outputs artifacts bridge

# Command to run the application using launch.py
CMD [".venv/bin/python", "launch.py"]
