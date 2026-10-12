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

Upgrade notes (v3):
    python manage.py migrate        # 0003: asset_code per unit (existing units are back-filled) + parent_line
    * Every physical piece (even "quantity" items) is a Unit with an internal asset code <item pk>-<n>.
    * Requests are dry-run against the real stock at SUBMIT time (unknown serial, wrong state/place,
      insufficient quantity are refused immediately, with the line number).
    * Issuing / reducing a complete server takes its installed parts with it (explicit child lines, voidable).
    * Manual test checklist: docs/TEST_CHECKLIST.md

Upgrade notes (v4):
    python manage.py migrate        # catalog 0002 (capacity), inventory 0004 (ChangeRequest)
    * Set "ظرفیت هر عدد" + unit on RAM/disk/CPU items (admin or the item edit page) to get totals on servers.
    * Edits of units/items are change requests that need approval; مدیر کل and superusers may approve their own.
