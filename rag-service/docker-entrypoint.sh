#!/bin/sh
# Load the corpus on first start (does nothing when the table already has rows), then serve.
set -e
python -m app.snapshot restore --if-empty
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
