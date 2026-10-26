"""Root conftest. Settings come from pyproject [tool.pytest.ini_options].

Prompt 02 adds shared fixtures in apps/core/testing.py; per-prompt tests
import from there.
"""

# NOTE: intentionally minimal for Prompt 01; fixtures accrue per prompt.
