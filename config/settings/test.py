# Fast local test settings (SQLite). NOTE: SQLite ignores select_for_update, so the
# true concurrency behaviour must be verified against MySQL (see docs).
import os
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only")
from .base import *  # noqa

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
