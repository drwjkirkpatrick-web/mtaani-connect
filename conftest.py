"""Root conftest. Settings come from pyproject [tool.pytest.ini_options].

Shared fixtures live in apps/core/testing.py (Prompt 02); pytest only
auto-registers fixtures from conftest files — re-export them here so every
test module can use them.
"""
from apps.core.testing import client_auth, fresh_user  # noqa: F401
