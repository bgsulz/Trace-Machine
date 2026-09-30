#!/bin/bash

echo "Starting deployment..."
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
echo "Fetching watermark decoder models (once; skipped if present)..."
flask download-models
echo "Running database migrations..."
flask db upgrade
echo "Restarting Gunicorn..."
sudo systemctl restart veracity
echo "Deployment complete!"