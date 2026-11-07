"""Analytics models (Prompt 37) — THE privacy guarantee, as a schema.

PITFALL PINNED HERE: "if a well-meaning contributor can add a user_id
column, the DPA exposure comes back." These two tables hold ONLY daily
counts:

* DailyAggregate — one row per (date x metric). Concrete columns are
  {id, date, metric, value} and tests/test_analytics.py asserts that
  equality EXACTLY: adding user_id / session_ref / ip / any user-identifiable
  field fails the suite before it can hold a byte of personal data.
* DailySaccoAggregate — the same daily count but split per SACCO (a business
  entity, like a plate — business data is allowed here, USER data never).
  Its concrete columns are exactly {id, date, sacco_id, sacco_name, value}.

What is structurally absent (not just conventionally absent): any
ForeignKey to the auth user, any session/token reference, any IP or device
or free-text field. value=None means "nothing observed yet" — an unknown
rate is NEVER recorded as 0 (the 0%-lie rule: never fabricate).
"""
from django.db import models
from django.utils.translation import gettext_lazy as _


class Metric(models.TextChoices):
    """The whole metric vocabulary (brief §6-8, aggregate-only).

    Each value names a daily count; WHERE each is computed from is
    documented door-by-door in apps/analytics/aggregator.py.
    """

    POSTS_CREATED = "posts_created", _("Posts created")
    GUIDES_PUBLISHED = "guides_published", _("Guides published")
    LISTINGS_CREATED = "listings_created", _("Listings created")
    MODERATION_QUEUE_OPEN = "moderation_queue_open", _("Moderation queue open")
    TRACKING_SESSIONS_STARTED = "tracking_sessions_started", _("Tracking sessions started")
    FEED_LOCAL_SHARE_PCT = "feed_local_share_pct", _("Feed local share (%)")
    DAU_ESTIMATE = "dau_estimate", _("Daily active users (estimate)")


class DailyAggregate(models.Model):
    """One daily number for one metric. NO user FK, NO session FK, NO IP,
    NO free text — a contributor cannot attach per-user tracking here
    without deleting a test first (and the pitfall test will then fail)."""

    date = models.DateField(_("Day (East Africa)"), db_index=True)
    metric = models.CharField(_("Metric"), max_length=32, choices=Metric.choices, db_index=True)
    # null = "no data observed for that day" (honest absence); an
    # unmeasurable share is stored as None, never as a fabricated 0.
    value = models.IntegerField(_("Value"), null=True, blank=True)

    class Meta:
        verbose_name = _("Daily aggregate")
        verbose_name_plural = _("Daily aggregates")
        ordering = ["-date", "metric"]
        constraints = [
            models.UniqueConstraint(
                fields=["date", "metric"], name="uniq_dailyagg_day_metric"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.date} {self.metric} = {self.value}"


class DailySaccoAggregate(models.Model):
    """Daily tracking-session count per SACCO.

    Saccos are a BUSINESS entity (the fleet owner, like a vehicle plate);
    business data is allowed in aggregates — USER data never is. The
    TrackingSession's authorized_by is deliberately NOT aggregated and is
    unreachable through this table.
    """

    date = models.DateField(_("Day (East Africa)"), db_index=True)
    # NOTE: sacco_id is a PLAIN NUMBER (not a Django FK) to routes.Sacco —
    # a business reference only, no cascade into user data; sacco_name is a
    # snapshot so the row stays readable if the SACCO row is later renamed.
    sacco_id = models.PositiveBigIntegerField(
        _("SACCO id"), null=True, blank=True, db_index=True
    )
    sacco_name = models.CharField(_("SACCO name"), max_length=120)
    value = models.IntegerField(_("Sessions started"), null=True, blank=True)

    class Meta:
        verbose_name = _("Daily SACCO aggregate")
        verbose_name_plural = _("Daily SACCO aggregates")
        ordering = ["-date", "sacco_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["date", "sacco_id"], name="uniq_dailysacco_day_sacco"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.date} {self.sacco_name} = {self.value}"
