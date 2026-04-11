import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Detect test runs so production-only security hardening (SSL redirect, HSTS,
# secure cookies) doesn't break Django's test client, which uses HTTP. Also
# used to force SQLite for tests so the suite doesn't require a running
# Postgres instance. Covers both `pytest` and `manage.py test` invocations.
_IS_TESTING = (
    'pytest' in sys.argv[0]
    or any('pytest' in arg for arg in sys.argv)
    or (len(sys.argv) > 1 and sys.argv[1] == 'test')
    or os.environ.get('PYTEST_CURRENT_TEST') is not None
)

# Security
SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-insecure-fallback-key-change-in-production')
DEBUG = os.environ.get('DEBUG', 'False').lower() in ('true', '1', 'yes')
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

# Required for Django 4.0+ when behind HTTPS reverse proxy (ALB → nginx)
_trusted = os.environ.get('CSRF_TRUSTED_ORIGINS', '')
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _trusted.split(',') if o.strip()]
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third-party
    'rest_framework',
    'drf_spectacular',
    'django_celery_results',
    'django_celery_beat',
    # Local
    'monitor',
    'django.contrib.humanize',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'monitor.middleware.TenantMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'arcwatch.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'arcwatch.wsgi.application'

# Database
# Always use SQLite during test runs so the suite doesn't need a running
# Postgres. Also use SQLite when USE_SQLITE=1 is explicitly set (e.g. local
# dev without Docker).
if _IS_TESTING or os.environ.get('USE_SQLITE', '0') == '1':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ.get('POSTGRES_DB', 'arcwatch'),
            'USER': os.environ.get('POSTGRES_USER', 'arcwatch'),
            'PASSWORD': os.environ.get('POSTGRES_PASSWORD', 'arcwatch'),
            'HOST': os.environ.get('POSTGRES_HOST', 'localhost'),
            'PORT': os.environ.get('POSTGRES_PORT', '5432'),
            # Use SQLite for the test runner even with PG config, so tests
            # don't require a running Postgres instance.
            'TEST': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': ':memory:',
            },
        }
    }

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Authentication
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

# Email (console backend for development; override in production via env vars)
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
DEFAULT_FROM_EMAIL = 'noreply@arcwatch.local'

# Django REST Framework
REST_FRAMEWORK = {
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

# drf-spectacular
SPECTACULAR_SETTINGS = {
    'TITLE': 'ArcWatch API',
    'DESCRIPTION': 'AI/ML Infrastructure GPU Monitoring Platform',
    'VERSION': '0.1.0',
}

# Celery
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = 'django-db'
CELERY_CACHE_BACKEND = 'django-cache'
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE

CELERY_BEAT_SCHEDULE = {
    "compute-cost-snapshot": {
        "task": "monitor.compute_cost_snapshot",
        "schedule": 60.0,
    },
    "evaluate-alert-rules": {
        "task": "monitor.evaluate_alert_rules",
        "schedule": 60.0,
    },
    "sync-llm-usage": {
        "task": "monitor.sync_llm_usage",
        "schedule": 3600.0,  # every hour
    },
    "sync-claude-code-usage": {
        "task": "monitor.sync_claude_code_usage",
        "schedule": 3600.0,  # every hour
    },
}

# Logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'json': {
            '()': 'pythonjsonlogger.jsonlogger.JsonFormatter',
            'format': '%(asctime)s %(name)s %(levelname)s %(message)s',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'json',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
}

# Security headers (production only — skipped under DEBUG and during test runs
# because Django's test client uses HTTP, which would otherwise 301 to HTTPS
# before any view runs).
if not DEBUG and not _IS_TESTING:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True

# Session timeout (8 hours)
SESSION_COOKIE_AGE = 28800
