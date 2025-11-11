FROM python:3.10-slim

# Avoid Python buffer problems
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

# Install system dependencies needed for building wheels (lxml, numpy, pandas, matplotlib, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    libssl-dev \
    libffi-dev \
    libxml2-dev \
    libxslt1-dev \
    libbz2-dev \
    liblzma-dev \
    zlib1g-dev \
    libblas-dev \
    liblapack-dev \
    pkg-config \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip setuptools wheel
RUN pip install -r /app/requirements.txt

# Copy application files
COPY . /app

# If there's a requirements file inside the `DISCORD BOT` subfolder, install it as well.
# This helps when the repository places dependencies inside that folder instead of root.
RUN if [ -f "/app/DISCORD BOT/requirements.txt" ]; then \
            python -m pip install -r "/app/DISCORD BOT/requirements.txt"; \
        fi

# Ensure prebuild script is executable (if present)
RUN if [ -f /app/prebuild.sh ]; then chmod +x /app/prebuild.sh; fi

# Default command
CMD ["python", "app.py"]
