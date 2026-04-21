"""Staging environment settings."""

import sentry_sdk

from .base import *  # noqa: F401, F403

DEBUG = False

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 3600
SECURE_HSTS_INCLUDE_SUBDOMAINS = True

DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"

sentry_sdk.init(
    dsn=env("SENTRY_DSN", default=""),  # noqa: F405
    traces_sample_rate=0.1,
    environment="staging",
)
