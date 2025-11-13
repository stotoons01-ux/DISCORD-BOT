#!/bin/sh
set -e

echo "[prebuild] Starting prebuild step"

# Ensure pip tooling
python -m pip install --upgrade pip setuptools wheel || true

# Install requirements if present
if [ -f requirements.txt ]; then
  echo "[prebuild] Installing requirements.txt"
  pip install -r requirements.txt || true
else
  echo "[prebuild] No requirements.txt found"
fi

# Try editable install so package imports (e.g. `db.mongo_adapters`) resolve
if [ -f setup.py ] || [ -f pyproject.toml ]; then
  echo "[prebuild] Attempting editable install of the project"
  pip install -e . || echo "[prebuild] editable install failed, continuing"
else
  echo "[prebuild] No setup.py or pyproject.toml found; skipping editable install"
fi

# Print Python and pip versions for debugging
python -V || true
pip -V || true

echo "[prebuild] Done"
