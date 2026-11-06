"""Migration 0002 — pg_trgm GIN indexes for the nickname search (Prompt 25).

WHY own indexes here: search.py runs raw trigram SQL against StageAlias
and the name columns; without GIN indexes every keystroke becomes a table
scan (the exact pitfall Prompts 14/25 both name and avoid). The pg_trgm
extension creation is idempotent (IF NOT EXISTS) so running after the
search app's identical statement is a no-op, never an error.
"""
from django.contrib.postgres.operations import CreateExtension
from django.db import migrations


INDEXES = """
CREATE INDEX IF NOT EXISTS routes_stagealias_alias_trgm
    ON routes_stagealias USING gin (alias gin_trgm_ops);
CREATE INDEX IF NOT EXISTS routes_stage_alias_name_trgm
    ON routes_stage USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS routes_route_name_en_trgm
    ON routes_route USING gin (name_en gin_trgm_ops);
CREATE INDEX IF NOT EXISTS routes_route_name_sw_trgm
    ON routes_route USING gin (name_sw gin_trgm_ops);
CREATE INDEX IF NOT EXISTS routes_sacco_name_trgm
    ON routes_sacco USING gin (name gin_trgm_ops);
"""

DROP_INDEXES = """
DROP INDEX IF EXISTS routes_stagealias_alias_trgm;
DROP INDEX IF EXISTS routes_stage_alias_name_trgm;
DROP INDEX IF EXISTS routes_route_name_en_trgm;
DROP INDEX IF EXISTS routes_route_name_sw_trgm;
DROP INDEX IF EXISTS routes_sacco_name_trgm;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("routes", "0001_initial"),
    ]

    operations = [
        CreateExtension("pg_trgm"),
        migrations.RunSQL(INDEXES, DROP_INDEXES),
    ]
