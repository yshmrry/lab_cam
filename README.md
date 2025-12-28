# Thermal Vision Monitor

## Server start command
- `gunicorn src.app:app -b 0.0.0.0:5000 --workers 1 --threads 4 --timeout 120`
