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
    "apps.profiles",
    "apps.guides",
    "apps.posts",
    "apps.qa",
    "apps.follows",
    "apps.groups",
    "apps.moderation",
    "apps.feed",
    "apps.search",
    "apps.trust",
    "apps.listings",
    "apps.messaging",
    "apps.services",
    "apps.barter",
    "apps.reviews",
    "apps.disputes",
    # Phase 3 begins (Prompt 25) while Prompt 19 closes Phase 2.
    "apps.routes",
    "apps.fleet",
    "apps.crowd",
    # Phase 3 wave 3 (Prompt 27 — privacy core of the flagship).
    "apps.tracking",
    # Phase 3 wave 4 (Prompt 28; 29 = apps/crew page + app).
    "apps.map",
    "apps.crew",
    # Phase 3 closer (Prompt 31).
    "apps.alerts",
    # Phase 4 (Prompt 32).
    "apps.traffic",
    # Phase 4 (Prompt 33).
    "apps.ai",
    # Phase 4 closers (Prompt 35's content-language prefs; 34 lives inside ai).
    "apps.lang",
    # Phase 5 (Prompts 37, 38; 36 = pure library in core).
    "apps.analytics",
    "apps.monetize",
    # Phase 5 wave 2 (Prompt 40; 39 = tests/scripts only, no app).
    "apps.compliance",
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
    # Prompt 41: CSP + nosniff/referrer/COOP headers on EVERY response.
    "apps.core.security_headers.SecurityHeadersMiddleware",
    # Prompt 36: systematic server-side throttles — LAST so a refused
    # request never reaches a view, and it needs request.user. One
    # enforcement site: the existing DB doors (moderation 10/h etc.) keep
    # their authority; this mirrors their numbers (test-pinned equal).
    "apps.core.throttle_django.ThrottleMiddleware",
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

# Session hygiene (Prompt 04; Prompt 41 hardening below).
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_HTTPONLY = True
# Prod MUST set SESSION_COOKIE_SECURE=1 (or any HTTPS cookie transport);
# a local http dev run keeps False so login works over plain localhost.
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=False)
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE

# Prompt 41 — transport/headers hardening (all overridable in prod env).
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)  # prod: 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Prompt 41 — CSP BEFORE tightening (the Leaflet pitfall): the map loads
# tiles from openstreetmap.org and Leaflet 1.9.4 from unpkg.com (SRI-pinned);
# the crew page's small inline script is why script-src carries
# 'unsafe-inline' for now — the NEXT hardening pass moves that inline JS to
# a nonce strategy (planned in docs/runbooks/security.md), NOT silently.
CONTENT_SECURITY_POLICY = {
    "default-src": ["'self'"],
    "img-src": ["'self'", "data:", "https://*.tile.openstreetmap.org"],
    "script-src": ["'self'", "https://unpkg.com", "'unsafe-inline'"],
    "style-src": ["'self'", "https://unpkg.com", "'unsafe-inline'"],
    "connect-src": ["'self'"],
    "base-uri": ["'self'"],
    "form-action": ["'self'"],
    "frame-ancestors": ["'none'"],
}

LOGIN_REDIRECT_URL = "home"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
