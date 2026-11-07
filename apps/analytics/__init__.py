"""Analytics app (Prompt 37) — aggregate-only ops metrics.

NOTE: this app exists to answer "is the app useful?" with numbers the DPA
can live with. Everything it stores is a DAILY COUNT keyed by (date x
category) — never a user, never a session, never an IP. The schema in
models.py IS the privacy guarantee: tests/test_analytics.py asserts the
concrete columns == exactly {id, date, metric, value}, so a well-meaning
contributor who adds a user_id column breaks the suite before the exposure
ever reaches a database.
"""
