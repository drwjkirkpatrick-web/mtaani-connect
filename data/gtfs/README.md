# data/gtfs/ — where the Digital Matatus GTFS zip lives

Drop **one or more** `.zip` archives here (e.g. the real 2018 Digital
Matatus feed). The `apps/routes` seeder (`apps/routes/gtfs.py`) picks up
any `*.zip` in this directory:

- `available()` (module function) → **False when this directory has no
  zip** — the network degrades gracefully: admin manual entry keeps
  working, search pages show the honest empty state.
- `GtfsSeeder(path).probe()` then `.seed()` import it; the import is
  idempotent (keyed by `gtfs_id`) and `override=False` by default so any
  admin/community repair in the DB is never reverted.

## The file is STALE seed data

The 2018 feed is years old. Everything it seeds is flagged
`seed_origin=True` and shows a **"seed-quality (2018 GTFS)"** badge in the
UI. Community corrections (wrong stage position / missing route) go to the
moderator queue (`/routes/queue/`, staff only) and admins edit the data
directly — the badge is not aspirational.

## What the loader probes (probes the ACTUAL file, never assumes)

The Nairobi feed is a *modified* GTFS. At load time each member's header is
read and logical fields are mapped from these candidates — first match wins,
and `seeder.probed` records what matched:

| member            | logical field | candidate columns tried                    |
|-------------------|---------------|--------------------------------------------|
| `routes.txt`      | route_id      | `route_id, rt_id, id`                      |
|                   | short/long    | `route_short_name, short_name, route_no, number / route_long_name, long_name, route_desc, name` |
|                   | color         | `route_color, color`                       |
|                   | agency_id     | `agency_id, sacco, agency`                 |
| `stops.txt`       | stop_id/name  | `stop_id, id` / `stop_name, name, stop_desc` |
|                   | lat/lon       | `stop_lat, lat, latitude` / `stop_lon, lon, lng, longitude` |
| `shapes.txt`      | shape_id      | `shape_id, id`                             |
|                   | lat/lon/seq   | `shape_pt_lat, lat, pt_lat` / `shape_pt_lon, lon, pt_lon, lng` / `shape_pt_sequence, seq, sequence` |
|                   | route_id      | `route_id` (Digital Matatus quirk — optional) |
| `trips.txt`       | standard      | `route_id, trip_id, shape_id`              |
| `stop_times.txt`  | standard      | `trip_id, stop_id, stop_sequence, seq, sequence` (optional) |
| `stop_aliases.txt`/`aliases.txt` | stop_id + alias | `stop_id` + `alias, nickname, name_alt` (optional) |

Unmatched columns are ignored, not guessed. Garbage numeric rows are
skipped, never "repaired".

## Honesty rules baked into the seeder

- **No fabricated shapes**: a route without shapes.txt data gets
  `shape_source=STAGE_TO_STAGE` (the UI draws ordered stage dots and says
  "approximate, NOT the real road path") — or NONE when stages lack
  coordinates. Never an invented line.
- **No fabricated fares**: GTFS carries none; fares come from admins and
  community observations ONLY, always as a min–max range with a
  `Confidence` label.
- **No fabricated stage order**: without `stop_times.txt`(+`trips.txt`),
  stages import unattached rather than in a made-up sequence.
- **Aliases only when the zip says so**: no alias is auto-derived from a
  stop name.

The committed test fixture (`tests/test_routes.py`) builds a tiny synthetic
zip at runtime — this directory does not need the real archive for the
test suite to pass.
