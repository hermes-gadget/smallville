"""Production settings for the public Smallville frontend."""

import os

from .base import *  # noqa: F401,F403


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if len(SECRET_KEY) < 50:
  raise RuntimeError("DJANGO_SECRET_KEY must contain at least 50 characters")

DEBUG = False
ALLOWED_HOSTS = [
  host.strip() for host in os.environ.get(
    "DJANGO_ALLOWED_HOSTS", "smallville.justarobot.uk").split(",")
  if host.strip()
]

# Cloudflare terminates TLS and forwards the original scheme.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
CSRF_TRUSTED_ORIGINS = ["https://smallville.justarobot.uk"]
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
