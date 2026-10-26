"""Test settings — used by pytest via pyproject [tool.pytest.ini_options].

WHY MD5 hasher: password hashing dominates fixture setup time; tests do not
need production-grade bcrypt cost.
"""
from .base import *  # noqa: F401,F403

DEBUG = False
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
