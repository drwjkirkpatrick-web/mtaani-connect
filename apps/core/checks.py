"""System check: PostGIS extension presence (PROMPTS.md Prompt 03).

WHY a WARNING, not an ERROR: the extension is installed BY migration
core.0001, so a fresh database legitimately lacks it until `migrate` runs.
An ERROR here would block that very migration (chicken-and-egg). The geo
TEST suite fails hard instead — a skipped geo suite is how geo bugs ship.
"""
from django.core import checks


@checks.register(checks.Tags.database)
def check_postgis_extension(app_configs, **kwargs):
    from django.db import connection

    messages = []
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'postgis'")
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - any failure becomes ONE readable message
        return [
            checks.Warning(
                f"Cannot verify the PostGIS extension (database unreachable: {exc}).",
                hint="Start the database (`make db-start` or `make up`) and re-run.",
                id="mtaani.W001",
            )
        ]
    if row is None:
        messages.append(
            checks.Warning(
                "PostGIS extension not found in database "
                f"'{connection.settings_dict.get('NAME')}'.",
                hint="Run `make migrate` — core.0001 installs it. Geo tests will fail until then.",
                id="mtaani.W002",
            )
        )
    return messages
