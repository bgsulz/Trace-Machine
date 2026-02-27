# Veracity

Veracity is a Flask app for image provenance analysis, with analyzers for C2PA, EXIF metadata, community consensus, and related tooling.

## Quick Start

1. Create and activate a virtual environment.
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Copy environment template:
   - `copy .env.example .env` (Windows)
   - `cp .env.example .env` (Unix)
4. Run the app:
   - `python run.py`

## Useful Scripts

- `python scripts/reset_db.py`
- `bash scripts/deploy.sh`

## Tests

- `pytest`

