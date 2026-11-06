"""apps.search — cross-entity full-text + trigram search (Prompt 14).

SearchDocument rows are maintained by Postgres triggers defined in this app's
migrations (see migrations/0002), NOT computed per query — see the NOTE/WHY
comments there.
"""
