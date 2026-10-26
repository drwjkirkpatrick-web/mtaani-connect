"""Base settings shared by every environment.

NOTE: environment-specific settings live in dev.py / test.py. Secrets and
machine-specific values come from .env (never committed; .env.example is
the manifest). WHY django-environ: one reader for os.environ + .env files.
"""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    environ.Env.read_env(_env_file)

SECRET_KEY = env("SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "testserver"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Mtaani apps — one Django app per prompt (PROMPTS.md hard rule).
    "apps.core",
    "apps.accounts",
    "apps.media",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "mtaani.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "mtaani.wsgi.application"

# WHY postgres not sqlite: PostGIS geography queries are core (PROMPTS.md hard
# rule). WHY plain engine: we use PostGIS via SQL functions on lat/lon columns,
# not GeoDjango model fields — keeps GDAL/GEOS system dependencies off every
# dev host. See docs/adr/0001-stack-decision.md amendments.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", default="mtaani"),
        "USER": env("DB_USER", default="mtaani"),
        "PASSWORD": env("DB_PASSWORD", default="mtaani_dev"),
        "HOST": env("DB_HOST", default="127.0.0.1"),
        "PORT": env("DB_PORT", default="5434"),
    },
}

# WHY day one: Django's custom-user trap — AUTH_USER_MODEL must exist before
# the first migrate; swapping later is effectively unrecoverable (Prompt 01).
AUTH_USER_MODEL = "accounts.User"

LANGUAGE_CODE = "en-us"
# Prompt 05: i18n shell — English + Kiswahili from day one (hard rule).
USE_I18N = True
LANGUAGES = [
    ("en", "English"),
    ("sw", "Kiswahili"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
# NOTE: display timezone; storage stays UTC because USE_TZ=True.
TIME_ZONE = "Africa/Nairobi"
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_ROOT = BASE_DIR / "mediafiles"
MEDIA_URL = "media/"

# Prompt 04: adapter wiring (mock-first; real adapters swap via env only).
SMS_ADAPTER = env("SMS_ADAPTER", default="apps.core.comms.MockSMS")
EMAIL_ADAPTER = env("EMAIL_ADAPTER", default="apps.core.comms.MockEmail")

# Session hygiene (Prompt 04; hardening continues in Prompt 41).
SESSION_COOKIE_SAMESITE = "Lax"

LOGIN_REDIRECT_URL = "home"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
