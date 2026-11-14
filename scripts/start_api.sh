#!/usr/bin/env bash
# Linux / macOS: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt first
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"
exec .venv/bin/python -m app.main
