#!/bin/bash
set -e  # Exit on any error

echo "Starting prebuild setup..."

# Function to log steps with timestamps
log_step() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Function to check command success
check_success() {
    if [ $? -eq 0 ]; then
        log_step "✓ $1 succeeded"
    else
        log_step "✗ $1 failed"
        exit 1
    fi
}

# Setup Python version
log_step "Setting up Python 3.10.13..."
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
if [ ! -d "$PYENV_ROOT" ]; then
    curl -L https://github.com/pyenv/pyenv-installer/raw/master/bin/pyenv-installer | bash
    eval "$(pyenv init -)"
    eval "$(pyenv virtualenv-init -)"
fi
pyenv install -s 3.10.13
pyenv global 3.10.13
check_success "Python setup"

# Verify Python version
PYTHON_VERSION=$(python --version)
log_step "Using $PYTHON_VERSION"

# Clear pip cache to avoid stale packages
log_step "Clearing pip cache..."
python -m pip cache purge
check_success "cache clear"

# Update pip and core build tools
log_step "Upgrading pip and build tools..."
python -m pip install --upgrade pip
check_success "pip upgrade"

log_step "Installing setuptools and wheel..."
python -m pip install --upgrade setuptools wheel
check_success "build tools installation"

# Install dependencies with detailed output
log_step "Installing project dependencies..."
python -m pip install -r requirements.txt --verbose
check_success "dependency installation"

# Verify critical packages
log_step "Verifying critical packages..."
python -c "import discord; print('discord.py version:', discord.__version__)"
python -c "import numpy; print('numpy version:', numpy.__version__)"
python -c "import pandas; print('pandas version:', pandas.__version__)"

log_step "Prebuild completed successfully!"