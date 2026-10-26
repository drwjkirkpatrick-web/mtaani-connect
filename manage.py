#!/usr/bin/env python
"""Django management entry point. Defaults to development settings."""
import os
import sys


def main():
    # NOTE: test runs override this via pyproject [tool.pytest.ini_options].
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mtaani.settings.dev")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
