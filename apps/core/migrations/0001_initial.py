"""Core migration 0001: install PostGIS into the Django-owned database.

WHY a model-free migration: dev AND test databases must both carry the
extension before any geo query runs; migrations are the one mechanism
pytest-django executes on the fresh test database automatically.

WHY noop reverse: extensions are shared database objects — a reversed
migration must never drop infrastructure other tools depend on.
"""
from django.db import migrations


class Migration(migrations.Migration):
    initial = True

    operations = [
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS postgis;",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
