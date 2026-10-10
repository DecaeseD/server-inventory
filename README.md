# Hosting Warehouse — Core

Setup:
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env   # fill values, then export them (or use your process manager)
    mysql> CREATE DATABASE warehouse CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
    python manage.py migrate
    python manage.py createsuperuser
    python manage.py verify_ledger

Tests (SQLite, fast):  DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test
Roles are created on migrate: مدیر کل / انباردار / حسابدار (accountant starts with ZERO permissions).
