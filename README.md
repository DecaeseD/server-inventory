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

Upgrade notes (v2):
    python manage.py migrate        # applies 0002 and creates the 3 default roles
    python manage.py setup_roles    # optional; --reset re-applies default role permissions
Assign users to a role (Group) in /admin/ > Users.
