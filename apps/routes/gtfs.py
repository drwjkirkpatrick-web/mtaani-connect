"""gtfs.py — Digital Matatus GTFS seeder (Prompt 25).

The 2018 Digital Matatus feed is STALE seed data, never ground truth:
- rows it creates are flagged ``seed_origin=True`` and stay editable in admin
  forever; re-seeding with the default ``override=False`` NEVER reverts an
  admin/community repair (edit-preservation is tested);
- it carries no fares (not part of GTFS), so nothing fare-ish is invented;
- shapes are imported ONLY from real shapes.txt rows — the PROMPTS.md 25
  pitfall "never fabricate missing shapes" is enforced here: a route whose
  feed has no line keeps ``shape_source=STAGE_TO_STAGE`` (view joins ordered
  stage dots AND LABELS IT APPROXIMATE) or ``NONE`` when stages lack coords.

PROBE, DON'T ASSUME: the Nairobi feed is a *modified* GTFS, so the loader
reads each member's header and maps logical fields from candidate names
(case-insensitive). What matched is recorded in ``seeder.probed`` —
data/gtfs/README.md documents the mapping so field quirks are discovered,
not assumed. Optional Digital Matatus extras handled:

- ``route_id`` inside shapes.txt (route→shape link without trips.txt);
- ``stop_aliases.txt`` / ``aliases.txt`` (stop_id, alias) — nicknames like
  "Ambassador" for the stage named "Ambassadeur". Without it NO alias is
  invented; communities add nicknames via admin/StageAlias and corrections.

Degradation contract: ``available() -> False`` when no zip exists; the admin
action shows the manual-entry path, which always stays open.
"""
from __future__ import annotations

import csv
import io
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from django.db import transaction

from .models import Route, RouteStage, Sacco, Stage, StageAlias

# Default zip location: <repo>/data/gtfs/*.zip. Constant (not a setting) so
# tests can point a per-call directory without touching parent-owned files.
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "gtfs"


def available(data_dir: str | Path | None = None) -> bool:
    """True when at least one .zip sits in the GTFS data dir."""
    directory = Path(data_dir) if data_dir else DATA_DIR
    return any(directory.glob("*.zip"))


def _clean(value: str | None) -> str:
    """Normalise one CSV value: strip BOM/whitespace, keep empties empty."""
    return (value or "").replace("\ufeff", "").strip()


class GtfsSeeder:
    """Import one Digital Matatus-format GTFS zip into the route network.

    Usage::

        seeder = GtfsSeeder(zip_path)
        if seeder.probe():        # headers read + validated on the spot
            seeder.seed()         # idempotent; override=False by default
    """

    # Logical field → candidate column names (probe order = priority order).
    _ROUTE_FIELDS = {
        "route_id": ["route_id", "rt_id", "id"],
        "short_name": ["route_short_name", "short_name", "route_no", "number"],
        "long_name": ["route_long_name", "long_name", "route_desc", "name"],
        "color": ["route_color", "color"],
        "agency_id": ["agency_id", "sacco", "agency"],
    }
    _STOP_FIELDS = {
        "stop_id": ["stop_id", "id"],
        "name": ["stop_name", "name", "stop_desc"],
        "lat": ["stop_lat", "lat", "latitude"],
        "lon": ["stop_lon", "lon", "lng", "longitude"],
    }
    _SHAPE_FIELDS = {
        "route_id": ["route_id"],  # Digital Matatus quirk — may be absent
        "shape_id": ["shape_id", "id"],
        "lat": ["shape_pt_lat", "lat", "pt_lat"],
        "lon": ["shape_pt_lon", "lon", "pt_lon", "lng"],
        "sequence": ["shape_pt_sequence", "seq", "sequence"],
    }
    _TRIP_FIELDS = {"route_id": ["route_id"], "trip_id": ["trip_id"], "shape_id": ["shape_id"]}
    _STOPTIME_FIELDS = {"trip_id": ["trip_id"], "stop_id": ["stop_id"],
                        "sequence": ["stop_sequence", "seq", "sequence"]}
    _ALIAS_FIELDS = {"stop_id": ["stop_id"], "alias": ["alias", "nickname", "name_alt"]}

    def __init__(self, zip_path: str | Path):
        self.zip_path = Path(zip_path)
        self.probed: dict[str, dict[str, str | None]] = {}
        # tables: "routes"/"stops"/"shapes"/"trips" keyed by their id;
        # "stop_times" → {trip_id: [rows]}; "aliases" → {stop_id: [rows]}.
        self.tables: dict[str, dict] = {}
        self.report: dict[str, int] = defaultdict(int)
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ load
    def _load(self, z: zipfile.ZipFile, filenames: set[str], fields: dict[str, list[str]],
              table: str, key_field: str, list_rows: bool = False) -> None:
        """Read one csv member, probe its header, store under `table`."""
        member = next(
            (n for n in z.namelist() if n.rsplit("/", 1)[-1].lower() in filenames), None
        )
        if member is None:  # optional file — the honest state is "absent"
            return
        with z.open(member) as fh:
            reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8-sig"))
            try:
                header = next(reader)
            except StopIteration:
                return
            lowered = {h.lower(): i for i, h in enumerate(header)}
        # Probe records the ORIGINAL casing of the column the file had
        # (quirks visible in seeder.probed, e.g. "Route_ID" stays visible).
        mapping = {logical: self._pick(header, cands) for logical, cands in fields.items()}
        self.probed[member.rsplit("/", 1)[-1].lower()] = mapping
        key_col = mapping.get(key_field)
        if key_col is None:  # cannot key — degrade, don't guess
            self.warnings.append(f"{member}: no usable {key_field} column")
            return
        # Rows read by INDEX below (short rows degrade to "" not IndexError).
        def _at(col: str | None, raw: list[str]) -> str:
            if col is None or len(raw) <= lowered[col.lower()]:
                return ""
            return _clean(raw[lowered[col.lower()]])
        with z.open(member) as fh:
            rows = csv.reader(io.TextIOWrapper(fh, encoding="utf-8-sig"))
            next(rows, None)  # skip the header row (probe consumed it)
            for raw in rows:
                if not raw or all(not _clean(c) for c in raw):
                    continue
                vals = {
                    logical: _at(col, raw)
                    for logical, col in mapping.items()
                }
                key = vals.get(key_field) or ""
                if not key:
                    continue
                if list_rows:
                    self.tables[table].setdefault(key, []).append(vals)
                else:
                    self.tables[table].setdefault(key, vals)  # first row wins

    @staticmethod
    def _pick(header: list[str], candidates: list[str]) -> str | None:
        """Original column name (probe record keeps the file's actual casing)."""
        for cand in candidates:
            for original in header:
                if original.lower() == cand:
                    return original
        return None

    def probe(self) -> bool:
        """Open the zip and probe header fields. False = unusable (no crash)."""
        self.tables.clear()
        self.probed.clear()
        self.report.clear()
        self.warnings.clear()
        if not self.zip_path.exists():
            return False
        try:
            with zipfile.ZipFile(self.zip_path) as z:
                self.tables = {"routes": {}, "stops": {}, "shapes": {}, "trips": {},
                               "stop_times": {}, "aliases": {}}
                self._load(z, {"routes.txt"}, self._ROUTE_FIELDS, "routes", "route_id")
                self._load(z, {"stops.txt"}, self._STOP_FIELDS, "stops", "stop_id")
                self._load(z, {"shapes.txt"}, self._SHAPE_FIELDS, "shapes", "shape_id",
                           list_rows=True)  # shapes.txt: ONE ROW PER POINT of a line
                self._load(z, {"trips.txt"}, self._TRIP_FIELDS, "trips", "trip_id")
                self._load(z, {"stop_times.txt", "stoptimes.txt"}, self._STOPTIME_FIELDS,
                           "stop_times", "trip_id", list_rows=True)
                self._load(z, {"stop_aliases.txt", "aliases.txt"}, self._ALIAS_FIELDS,
                           "aliases", "stop_id", list_rows=True)
        except (zipfile.BadZipFile, OSError, UnicodeDecodeError, csv.Error, EOFError):
            return False
        return bool(self.tables.get("routes")) and bool(self.tables.get("stops"))

    # ---------------------------------------------------------------- shapes
    def _shapes_by_route(self) -> dict[str, list[list[float]]]:
        """route_id → ordered [(lon, lat), ...] points of its busiest shape.

        WHY busiest variant: a corridor can carry several trip variants
        (detours); picking the most-referenced variant makes the import
        deterministic instead of order-dependent.
        """
        by_shape: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
        route_of_shape: dict[str, Counter] = defaultdict(Counter)
        for sid, rows in self.tables.get("shapes", {}).items():
            for row in rows:
                try:
                    latf, lonf = float(row.get("lat")), float(row.get("lon"))
                    seq = int(row.get("sequence") or 0)
                except (TypeError, ValueError):
                    continue  # broken point: skipped, never repaired into a guess
                by_shape[sid].append((seq, lonf, latf))
                # Digital Matatus quirk: a route_id column right in shapes.txt.
                rid = row.get("route_id")
                if rid:
                    route_of_shape[rid][sid] += 1
        # Standard GTFS fallback: trips.txt maps trip → (route, shape).
        for _, trow in self.tables.get("trips", {}).items():
            rid, shid = trow.get("route_id"), trow.get("shape_id")
            if rid and shid and shid in by_shape:
                route_of_shape[rid][shid] += 1
        out: dict[str, list[list[float]]] = {}
        for rid, counter in route_of_shape.items():
            best = counter.most_common(1)[0][0]
            ordered = sorted(by_shape[best])  # by shape_pt_sequence
            pts = [(lon, lat) for _, lon, lat in ordered]
            if len(pts) >= 2:  # a single point is not a line — drop it
                out[rid] = [[round(lon, 6), round(lat, 6)] for lon, lat in pts]
        return out

    def _order_by_route(self) -> dict[str, list[str]]:
        """route_id → stop_ids in order, from stop_times via trips.

        NOTE: order comes from the trip with the MOST stop visits per route.
        Without stop_times/trips the import leaves routes stage-less rather
        than fabricating a sequence — admins add RouteStage links by hand.
        """
        trips = self.tables.get("trips", {})
        stop_times = self.tables.get("stop_times", {})
        if not (trips and stop_times):
            return {}
        visits_per_trip: dict[str, Counter] = defaultdict(Counter)
        stages_of_trip: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for tid, rows in stop_times.items():
            for row in rows:
                try:
                    seq = int(row.get("sequence") or 0)
                except (TypeError, ValueError):
                    continue
                sid = row.get("stop_id")
                if sid:
                    stages_of_trip[tid].append((seq, sid))
            visits_per_trip[trips.get(tid, {}).get("route_id", "")][tid] = 0
        for tid, rows in stages_of_trip.items():
            visits_per_trip[trips.get(tid, {}).get("route_id", "")][tid] = len(rows)
        out: dict[str, list[str]] = {}
        for rid, counter in visits_per_trip.items():
            if not rid:
                continue
            best = counter.most_common(1)[0][0] if counter else ""
            rowlist = stages_of_trip.get(best) or []
            ordered = [sid for _, sid in sorted(rowlist)]
            if ordered:
                out[rid] = ordered
        return out

    # ----------------------------------------------------------------- seed
    def seed(self, override: bool = False, progress=None) -> dict[str, int]:
        """Idempotent import. ``override=False`` (default) never reverts rows
        an admin edited later; ``override=True`` re-stamps GTFS values.

        ``progress(stage_label)`` is optional (admin action UI shows steps).
        """
        if progress:
            progress("probing")
        if not self.probe():
            self.report["error"] = 1
            return self.report
        if progress:
            progress("importing")
        self._seed(override)
        if progress:
            progress("done")
        return dict(self.report)

    @transaction.atomic
    def _seed(self, override: bool) -> None:
        report = self.report

        # 1. Stages: upsert by gtfs stop_id. An existing row the admin fixed
        #    is left alone unless override=True (edit preservation).
        stops_by_gtfs: dict[str, Stage] = {}
        for sid, row in self.tables.get("stops", {}).items():
            lat, lon = row.get("lat"), row.get("lon")
            try:
                latf, lonf = float(lat), float(lon)
            except (TypeError, ValueError):
                latf, lonf = None, None  # honestly "no coordinates", not a guess
            defaults: dict = {"name": row.get("name") or sid, "seed_origin": True}
            if latf is not None:
                defaults["lat"], defaults["lon"] = latf, lonf
            stage, created = self._upsert(Stage, dict(gtfs_id=sid), defaults, override)
            stops_by_gtfs[sid] = stage
            report["stages_created" if created else "stages_present"] += 1

        # 2. Stage aliases from the optional alias file (or none at all).
        for sid, rows in self.tables.get("aliases", {}).items():
            stage = stops_by_gtfs.get(sid)
            if stage is None:
                continue
            for row in rows:
                alias = row.get("alias")
                if alias:
                    StageAlias.objects.get_or_create(
                        stage=stage, alias=alias, defaults={}
                    )
                    report["aliases_present"] += 1

        # 3. Routes, with real shapes only.
        shapes_by_route = self._shapes_by_route()
        for rid, row in self.tables.get("routes", {}).items():
            name_en = row.get("long_name") or row.get("short_name") or rid
            name_sw = row.get("short_name") or ""  # feed spelling, no invention
            route_points = shapes_by_route.get(rid)
            if route_points:
                src = Route.ShapeSource.GTFS
            else:
                src = Route.ShapeSource.STAGE_TO_STAGE
            defaults = {
                "name_en": name_en,
                "name_sw": name_sw,
                "corridor_color": row.get("color") or "",
                "seed_origin": True,
                "shape_source": src,
                "shape_points": route_points or None,
            }
            # NOTE defaults never include continuous_stop / saccos — those are
            # human-curated, so even override=True cannot stamp them away.
            route, created = self._upsert(Route, dict(gtfs_id=rid), defaults, override)
            report["routes_created" if created else "routes_present"] += 1

            # 4. SACCO link from agency_id (name/contact stay blank: the feed
            #    has none — staff curate; never a fabricated contact).
            agency = row.get("agency_id")
            if agency:
                sacco, _ = Sacco.objects.get_or_create(
                    gtfs_id=agency, defaults={"name": agency}
                )
                route.saccos.add(sacco)

            # 5. Ordered stages (RouteStage) — only with real trip data.
            order = self._order_by_route().get(rid) or []
            for position, sid in enumerate(order, start=1):
                stage = stops_by_gtfs.get(sid)
                if stage is None:
                    continue
                # WHY get_or_create on (route, stage): re-runs keep the link
                # an admin may have repositioned (position not overwritten).
                link, link_created = RouteStage.objects.get_or_create(
                    route=route, stage=stage, defaults={"position": position}
                )
                if link_created:
                    report["routestages_created"] += 1

    @staticmethod
    def _upsert(model, lookup: dict, defaults: dict, override: bool = False) -> tuple:
        """Create-or-update preserving manual edits.

        WHY this exists: the 2018 feed is stale — anything an admin fixed
        (name, coordinates, colour...) must survive a re-seed with
        override=False. override=True is the explicit "re-stamp" path used
        by admin tooling and tests. Fields the seeder never writes
        (Stage.continuous_stop, Sacco.contact_info, saccos M2M) simply stay
        out of ``defaults`` — even override=True cannot wipe human curation.
        """
        obj, created = model.objects.get_or_create(**lookup, defaults=defaults)
        if created:
            return obj, True
        if override:
            for field, value in defaults.items():
                setattr(obj, field, value)
            obj.save()
        return obj, False
