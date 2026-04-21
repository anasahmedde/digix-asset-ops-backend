# DIGIX Asset Ops -- Backend

Django REST API for the DIGIX Asset Management & Operations Platform.

## Stack

- Python 3.12, Django 5.x, Django REST Framework
- PostgreSQL, Celery + Redis, Django Channels (WebSocket)
- JWT authentication, S3 file uploads, QR/barcode generation

## Quick Start

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements/local.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## API Docs

http://localhost:8000/api/docs/ (Swagger UI)

## Docker

```bash
docker build -f Dockerfile -t digix-backend .
docker run -p 8000:8000 digix-backend
```

## Apps

14 Django apps covering 23 capabilities: accounts, assets, sites, tickets, teams, warranties, maintenance, infrastructure, inventory, suppliers, clients, procurement, finance, analytics.
