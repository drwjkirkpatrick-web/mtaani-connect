"""Aggregate recompute service (Prompt 37) — reads doors, writes only here.

`rebuild_daily(day)` is the ONLY write path in the app. Each metric is
computed from the owning app's PUBLIC door where a door serves the metric;
where a door exists but answers a different question (visibility lists hide
drafts/removed — an OPS activity count must include them), the fallback is
a READ-ONLY manager query on that app's model, never a write, and the
reason is annotated below. Cross-app imports are lazy inside the functions
so app-loading order can never bite.

Day semantics: TIME_ZONE is Africa/Nairobi (USE_TZ=True) — `__date`
lookups convert UTC stamps to Nairobi days, so "daily" means the local
operator's day, matching every other report in the project.

No per-user query is possible here by construction: every helper returns a
COUNT or an aggregate, keyed by day (and sacco), nothing else.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.utils import timezone

from apps.analytics.models import DailyAggregate, DailySaccoAggregate, Metric

__all__ = ["rebuild_daily", "window_metrics", "queue_status", "sacco_session_hours"]


# ---------------------------------------------------------------- daily rows
def _rebuild_posts_created(day: date) -> int:
    """Posts created on `day` — ALL statuses (a draft was still written).

    NOTE: posts' PUBLIC door `visible_posts()` answers "what the feed may
    show" (no DRAFT/UNDER_REVIEW/REMOVED) — an activity count deliberately
    differs, so this is a read-only manager count. Only the count is lifted;
    no author, no text.
    """
    from apps.posts.models import Post

    return Post.objects.filter(created_at__date=day).count()


def _rebuild_guides_published(day: date) -> int:
    """Guide revisions flipped PUBLISHED on `day` — Prompt 08's publish
    stamps `published_at` on the revision, which IS the publish moment
    (guide containers may sit published for days). Manager read: guides'
    door `visible_guides(for_user)` needs a viewer and returns today's
    published set, not a per-day publish history."""
    from django.db.models import Q

    from apps.core.types import ContentStatus
    from apps.guides.models import GuideRevision

    return GuideRevision.objects.filter(
        status=ContentStatus.PUBLISHED.value, published_at__date=day
    ).count()


def _rebuild_listings_created(day: date) -> int:
    """Listings created on `day` — ALL lifecycle states (`visible_listings`
    is the shopper-facing door; operators count creation activity)."""
    from apps.listings.models import Listing

    return Listing.objects.filter(created_at__date=day).count()


def _rebuild_moderation_queue_open() -> int:
    """The live queue load (OPEN + UNDER_REVIEW) read THROUGH the
    moderation PUBLIC door `queue_reports()` — no manager shortcut."""
    from apps.moderation.services import queue_reports

    return queue_reports().count()


def _rebuild_tracking(day: date) -> tuple[int, list[tuple[int | None, str, int]]]:
    """Tracking sessions STARTED on `day`, total + grouped per SACCO.

    Tracking exposes no aggregate door (its doors are per-session for
    privacy), so this is a read-only count/group on the session rows. The
    grouping extracts vehicle__sacco only — a BUSINESS key; authorized_by
    (the authority record) is never read.
    """
    from apps.tracking.models import TrackingSession

    qs = TrackingSession.objects.filter(started_at__date=day)
    total = qs.count()
    per_sacco = (
        TrackingSession.objects.filter(started_at__date=day)
        .values("vehicle__sacco_id", "vehicle__sacco__name")
        .annotate(n=Count("pk"))
        .order_by("vehicle__sacco_id")
    )
    rows = [
        (g["vehicle__sacco_id"], g["vehicle__sacco__name"], g["n"]) for g in per_sacco
    ]
    return total, rows


def _rebuild_feed_local_share(day: date, *, now: Any) -> int | None:
    """% of the day's feed items that are LOCAL-utility (the brief's
    utility metric — NOT time-on-app).

    Read THROUGH the feed PUBLIC door `assemble_rows(viewer, now)` (ranked
    rows, exactly what /feed/ shows for an unauthenticated visitor — a block
    list cannot exist for nobody, so anonymity keeps this viewer-neutral).
    An item is "local" when it targets local content: a linked guide
    (apps.guides — Module 3 local knowledge) or a linked listing
    (apps.listings — local commerce). Plain social posts are not local.

    Returns None when fewer than one row exists for the day — NEVER a
    fabricated 0%: unknown is unknown (spec: never fabricate).
    """
    from django.contrib.auth.models import AnonymousUser

    from apps.feed.services import assemble_rows

    # An AnonymousUser mirrors exactly what a logged-out /feed/ visitor
    # sees, and every upstream door branch on is_authenticated — no User
    # row is ever touched (no lookup, no block join, nothing per-person).
    rows = assemble_rows(AnonymousUser(), usefulness_scorer=None, now=now)
    day_rows = [r for r in rows if timezone.localdate(r["created_at"]) == day]
    total = len(day_rows)
    if not total:
        return None
    local = sum(
        1
        for r in day_rows
        if r["ref_guide_id"] is not None or r["ref_listing"] is not None
    )
    return round(100 * local / total)


def _rebuild_dau(day: date) -> int:
    """Honest-scope DAU v1: distinct members whose LAST login fell on
    `day` (auth's own last_login — no per-day activity table exists, and
    per-user activity tracking would need exactly the per-user rows this
    app refuses to build). A member active but not re-logging-in is not
    counted; the metric is a floor, documented as an estimate everywhere."""
    User = get_user_model()
    return User.objects.filter(last_login__date=day).count()


def rebuild_daily(day: date, *, now: Any | None = None) -> list[DailyAggregate]:
    """Recompute every metric for `day` and persist (overwrite, never
    duplicate). Idempotent: safe to run repeatedly (cron) — an idempotent
    re-run updates the row, it does not append a second one.

    Returns the stored DailyAggregate rows in Metric order.
    """
    now = timezone.now() if now is None else now

    tracking_total, per_sacco = _rebuild_tracking(day)
    feed_share = _rebuild_feed_local_share(day, now=now)
    computed: dict[str, int | None] = {
        Metric.POSTS_CREATED.value: _rebuild_posts_created(day),
        Metric.GUIDES_PUBLISHED.value: _rebuild_guides_published(day),
        Metric.LISTINGS_CREATED.value: _rebuild_listings_created(day),
        Metric.MODERATION_QUEUE_OPEN.value: _rebuild_moderation_queue_open(),
        Metric.TRACKING_SESSIONS_STARTED.value: tracking_total,
        Metric.FEED_LOCAL_SHARE_PCT.value: feed_share,
        Metric.DAU_ESTIMATE.value: _rebuild_dau(day),
    }

    stored: list[DailyAggregate] = []
    for metric, value in computed.items():
        obj, _created = DailyAggregate.objects.update_or_create(
            date=day, metric=metric, defaults={"value": value}
        )
        stored.append(obj)

    DailySaccoAggregate.objects.filter(date=day).delete()
    for sacco_id, sacco_name, n in per_sacco:
        DailySaccoAggregate.objects.create(
            date=day, sacco_id=sacco_id, sacco_name=sacco_name, value=n
        )
    return stored


def window_metrics(days: int, *, today: date) -> dict[str, int | None]:
    """Dashboard window view over the DAILY rows: per METRIC either the sum
    of the window's non-None daily counts (volume metrics) or the mean
    daily percentage (FEED_LOCAL_SHARE_PCT — a share cannot be summed). A
    metric with no observable rows reports None — never a fabricated 0."""
    from collections import defaultdict
    from datetime import timedelta

    start = today - timedelta(days=days - 1)
    values: dict[str, list[int]] = defaultdict(list)
    for row in DailyAggregate.objects.filter(date__gte=start, date__lte=today):
        if row.value is not None:
            values[row.metric].append(row.value)
    window: dict[str, int | None] = {}
    for metric in Metric:
        daily = values.get(metric.value)
        if not daily:
            window[metric.value] = None
        elif metric is Metric.FEED_LOCAL_SHARE_PCT:
            window[metric.value] = round(sum(daily) / len(daily))
        else:
            window[metric.value] = sum(daily)
    return window


def queue_status(*, now: Any | None = None) -> dict[str, int | float | None]:
    """Moderator-queue numbers read THROUGH the moderation PUBLIC door:
    open count, under-review count, and the age (hours) of the OLDEST OPEN
    report — the "queue age" ops number. None age ⇔ nothing open."""
    from datetime import timedelta

    from apps.moderation.services import queue_reports
    from apps.moderation.models import ReportStatus

    now = timezone.now() if now is None else now
    qs = queue_reports()
    oldest_open = (
        qs.filter(status=ReportStatus.OPEN.value).order_by("created_at").first()
    )
    data: dict[str, int | float | None] = {
        "open": qs.filter(status=ReportStatus.OPEN.value).count(),
        "under_review": qs.filter(status=ReportStatus.UNDER_REVIEW.value).count(),
        "oldest_open_age_hours": None,
    }
    if oldest_open is not None:
        data["oldest_open_age_hours"] = round(
            (now - oldest_open.created_at).total_seconds() / 3600, 1
        )
    return data


def sacco_session_hours(
    sacco, days: int, *, now: Any | None = None
) -> dict[str, int | float | None]:
    """One SACCO's fleet session HOURS over the window — the SACCO
    operator's own-fleet number (tenant-scoped by the CALLER through the
    fleet PUBLIC doors, never computed cross-sacco here).

    Closed sessions contribute (ends_at - started_at); live sessions
    contribute (now - started_at) as elapsed-so-far. Returns hours (2 dp),
    counts closed/live, and per-day started counts from the aggregate rows.
    """
    from datetime import timedelta

    from apps.tracking.models import TrackingSession

    now = timezone.now() if now is None else now
    window_start = now - timedelta(days=days)
    sessions = TrackingSession.objects.filter(
        vehicle__sacco_id=sacco.pk, started_at__gte=window_start
    )
    seconds = 0.0
    closed = 0
    live = 0
    for session in sessions:
        end = session.ends_at
        if end is None:
            live += 1
            end = now
        else:
            closed += 1
        seconds += max(0.0, (end - session.started_at).total_seconds())
    by_day = list(
        DailySaccoAggregate.objects.filter(
            date__gte=timezone.localdate(window_start), date__lte=timezone.localdate(now),
            sacco_id=sacco.pk,
        ).order_by("date")
    )
    return {
        "sacco": sacco,
        "hours": round(seconds / 3600, 2),
        "closed": closed,
        "live": live,
        "by_day": by_day,
    }
