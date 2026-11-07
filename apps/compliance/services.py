"""services.py — Prompt 40's operations: DSAR export, deletion (KDPA
right-to-erasure vs legal-retention balance), the registry loader, the DSAR
state machine.

THE DELETION TRADE-OFF (read before touching anonymize_account — the
reasoning lives here ON PURPOSE, not only in the DPIA):

KDPA balances the data subject's right to erasure against the rights of
others and legal retention duties. ``anonymize_account`` therefore:

  * ERASES the identity: username -> ``anon-<sha256[:16h] of PK>`` (prefix
    + hex digest — deterministic off the IMMUTABLE pk, never derived from
    the old username, so it cannot be reversed to who it was), names/
    email/phone blanked, password made unusable, account deactivated,
    live sessions deleted, profile identity text blanked.
  * REMOVES the member's PUBLISHABLE personal content through
    moderation.services.apply_removal (the status door every surface
    already obeys): posts, Q&A questions, listings, services flip to
    REMOVED — rows SURVIVE as the evidence moderation/disputes may still
    need. Guides AND qa answers are deliberately EXCLUDED (shared/participatory
    community assets; see the inventory notes on guides.Guide +
    qa.Answer for the written trade-off).
  * PRESERVES, with the lawful reason recorded in the matching INVENTORY
    note: moderation Reports + ModeratorAudit/FleetAudit rows (dispute
    evidence + audit trails: FKs keep resolving, now to an anonymous
    actor), consent events (legal obligation), disputes/reviews/barter
    (financial-adjacent transaction evidence owed to the OTHER party),
    messaging bodies (joint personal data of all thread participants),
    search documents (DB triggers rebuild them from the erased rows).
  * IDEMPOTENT: a second call raises AlreadyAnonymized and writes nothing.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.compliance.inventory import (
    inventory_rows,
    validate_inventory_shape,
)
from apps.compliance.models import (
    DataMapEntry,
    DataRequest,
    LawfulBasis,
    RequestStatus,
)

ANON_PREFIX = "anon-"
_ANON_PATTERN = re.compile(r"^anon-[0-9a-f]{16}$")

# Content the deletion flow REMOVES via moderation's status door: rows keep
# existing (evidence) but exit every published surface. Deliberately NOT
# here: guides AND qa.Answer — both are shared/participatory community
# assets without a removal contract (Answer has no status column; removal
# would strand accepted-answer references). See the inventory notes.
_REMOVAL_TARGETS: tuple[tuple[str, str, str], ...] = (
    # (model_ref, user-FK field, counts label)
    ("posts.Post", "author", "posts_removed"),
    ("qa.Question", "asked_by", "questions_removed"),
    ("listings.Listing", "seller", "listings_removed"),
    ("services.ServiceListing", "provider", "services_removed"),
)


class AlreadyAnonymized(ValidationError):
    """The account is already anonymized — second refusal is BY DESIGN
    (tests pin that the retry changes nothing)."""


def is_anonymized(user) -> bool:
    """True when the username matches the dedicated anon shape."""
    return bool(_ANON_PATTERN.match(user.username or ""))


def anon_code_for(user) -> str:
    """Deterministic anonymous username: sha256 off the account PK."""
    digest = hashlib.sha256(f"mtaani-erasure:{user.pk}".encode()).hexdigest()
    return ANON_PREFIX + digest[:16]


# --------------------------------------------------------------------- load
def load_inventory() -> dict[str, int]:
    """Upsert the hand-maintained INVENTORY into DataMapEntry rows.

    WHY a runtime loader: the inventory is a living document (later
    prompts land columns); upsert-from-code keeps the registry moving
    with code, and tests/test_compliance.py pins parity both ways. A data
    migration also seeds a fresh DB at migrate time (prompt-40 module
    contract: the map is self-documenting out of the box).
    """
    problems = validate_inventory_shape()
    if problems:
        raise ValueError(f"INVENTORY shape invalid: {problems}")

    created = updated = 0
    for row in inventory_rows():
        entry, was_created = DataMapEntry.objects.update_or_create(
            app_label=row["app"],
            model_name=row["model"],
            defaults={
                "fields": row["fields"],
                "purpose": row["purpose"],
                "lawful_basis": LawfulBasis(row["basis"]).value,
                "retention_days": row["retention"],
                "notes": row.get("note", ""),
            },
        )
        created += 1 if was_created else 0
        updated += 0 if was_created else 1
    return {"created": created, "updated": updated}


def datamap_entries() -> "list[DataMapEntry]":
    """Loaded-in-memory map rows (views/tests call load_inventory first)."""
    load_inventory()
    return list(DataMapEntry.objects.all().select_related())


# ------------------------------------------------------------------- export
def export_archive(user, *, now: datetime | None = None) -> str:
    """The readable .txt archive of ONE member's data (DSAR access).

    Scope: what the apps hold ABOUT the requester — identity, profile,
    consent ledger (incl. their own source_ip), their DSAR rows, their
    content verbatim (titles/bodies), their own words in threads, graph/
    alert follows, offers' role, badges. Third parties' rows are NOT
    included: the access right covers the requester's data, not the
    fishing net (KDPA). Rendered plain text on purpose — a human reads
    this, no JSON archaeology.
    """
    now = now or timezone.now()
    lines: list[str] = []
    say = lines.append
    say("=" * 62)
    say("MTAANI CONNECT — YOUR DATA (KDPA data-subject access export)")
    say(f"Generated: {now:%Y-%m-%d %H:%M} UTC")
    say(f"Account: {user.username} (pk {user.pk})")
    say("=" * 62)

    say("")
    say("## ACCOUNT")
    say(f"username: {user.username}")
    if user.first_name or user.last_name:
        say(f"name: {user.first_name} {user.last_name}".rstrip())
    say(f"email: {user.email}")
    say(f"phone: {user.phone}")
    say(f"joined: {user.date_joined:%Y-%m-%d}")
    if user.last_login:
        say(f"last_login: {user.last_login:%Y-%m-%d %H:%M}")

    profile = getattr(user, "profile", None)
    if profile:
        say("")
        say("## PROFILE")
        say(f"display_name: {profile.display_name}")
        say(f"bio: {profile.bio}")
        say(f"ward: {profile.ward}")
        say(f"skills: {profile.skills}")
        say(f"interests: {profile.interests}")
        say(f"location_visibility: {profile.location_visibility}")
        say(f"precise_location_consent: {profile.precise_location_consent}")

    say("")
    say("## CONSENTS (document versions you accepted)")
    ledger = user.consent_events.all().order_by("created_at")
    rows = list(ledger)
    for ce in rows:
        say(
            f"- {ce.created_at:%Y-%m-%d %H:%M} {ce.purpose_key} v{ce.doc_version}"
            + (f" from {ce.source_ip}" if ce.source_ip else "")
        )

    say("")
    say("## DATA REQUESTS")
    req_rows = list(user.data_requests.all().order_by("-created_at"))
    if not req_rows:
        say("- none")
    for r in req_rows:
        suffix = f" ({r.note})" if r.note else ""
        say(f"- {r.created_at:%Y-%m-%d} {r.kind} {r.status}{suffix}")

    from apps.posts.models import Post
    from apps.qa.models import Question, Answer
    from apps.listings.models import Listing
    from apps.services.models import ServiceListing

    say("")
    say("## POSTS")
    posts = list(Post.objects.filter(author=user).order_by("created_at"))
    if not posts:
        say("- none")
    for p in posts:
        say(f"- [{p.status}] ({p.created_at:%Y-%m-%d})")
        say(f"  {p.text}")

    say("")
    say("## QUESTIONS")
    questions = list(Question.objects.filter(asked_by=user).order_by("created_at"))
    if not questions:
        say("- none")
    for q in questions:
        say(f"- [{q.status}] {q.title_en}")
        say(f"  {q.body}")

    say("")
    say("## ANSWERS")
    answers = list(Answer.objects.filter(author=user).order_by("created_at"))
    if not answers:
        say("- none")
    for a in answers:
        say(f"- [{a.status}] {a.body}")

    say("")
    say("## LISTINGS")
    listings = list(Listing.objects.filter(seller=user).order_by("created_at"))
    if not listings:
        say("- none")
    for l in listings:
        say(f"- [{l.status}] {l.title} — ward {l.ward}, price {l.price}")
        say(f"  {l.description}")

    say("")
    say("## SERVICES")
    services = list(ServiceListing.objects.filter(provider=user).order_by("created_at"))
    if not services:
        say("- none")
    for s in services:
        say(f"- [{s.status}] {s.title} — ward {s.ward}")
        say(f"  {s.description}")

    from apps.messaging.models import Message

    say("")
    say("## MESSAGES (your words in threads)")
    mine = list(Message.objects.filter(sender=user).order_by("created_at"))
    if not mine:
        say("- none")
    for m in mine:
        say(f"- ({m.created_at:%Y-%m-%d %H:%M}) {m.body}")

    from apps.follows.models import Follow
    from apps.groups.models import GroupMembership

    say("")
    say("## FOLLOWS (people)")
    follow_rows = list(Follow.objects.filter(follower=user) | Follow.objects.filter(followee=user))
    if not follow_rows:
        say("- none")
    for f in follow_rows:
        if f.follower_id == user.pk:
            # The followee's NAME belongs to the followee — export resolves
            # only the requester's side of the edge.
            say(f"- you follow member pk {f.followee_id}")
        else:
            say(f"- followed by member pk {f.follower_id}")

    say("")
    say("## GROUPS")
    gm_rows = list(GroupMembership.objects.filter(user=user).select_related("group"))
    if not gm_rows:
        say("- none")
    for gm in gm_rows:
        say(f"- {gm.group.name} as {gm.role}")

    from apps.alerts.models import RouteFollow as _RF
    from apps.alerts.models import VehicleFollow as _VF

    say("")
    say("## ALERTS (vehicle/route watches)")
    for vf in _VF.objects.filter(user=user):
        say(f"- vehicle pk {vf.vehicle_id} · {vf.radius_km}km · "
            f"{'on' if vf.notify_enabled else 'off'}")
    for rf in _RF.objects.filter(user=user):
        say(f"- route pk {rf.route_id} · {rf.radius_km}km · "
            f"{'on' if rf.notify_enabled else 'off'}")

    from apps.barter.models import Offer
    from apps.trust.models import BadgeEvent

    say("")
    say("## BARTER OFFERS (your role in each trade)")
    offer_rows = list(
        (Offer.objects.filter(initiator=user) | Offer.objects.filter(counterpart=user))
    )
    if not offer_rows:
        say("- none")
    for o in offer_rows:
        side = "initiator" if o.initiator_id == user.pk else "counterpart"
        say(f"- offer pk {o.pk}: {side}, status {o.status}")

    say("")
    say("## BADGES")
    badges = list(BadgeEvent.objects.filter(user=user))
    if not badges:
        say("- none")
    for b in badges:
        say(f"- {b.category} {b.level} ({b.awarded_at:%Y-%m-%d})")

    say("")
    say("END")
    return "\n".join(lines)


# ---------------------------------------------------------------- deletion
def _kill_sessions(user_pk: int, now: datetime) -> int:
    """Delete the account's live login sessions (an anonymized account must
    keep no session: deactivation alone does not drop an open browser)."""
    from django.conf import settings as dj_settings
    from importlib import import_module

    engine = import_module(dj_settings.SESSION_ENGINE)
    try:
        from django.contrib.sessions.models import Session
    except ImportError:  # non-DB session backend: see DPIA residual risks
        return 0
    killed = 0
    decoder = engine.SessionStore()
    for sess in Session.objects.filter(expire_date__gt=now):
        data = decoder.decode(sess.session_data) or {}
        if str(data.get("_auth_user_id", "")) == str(user_pk):
            sess.delete()
            killed += 1
    return killed


@transaction.atomic
def anonymize_account(
    user, *, now: datetime | None = None, reason: str = ""
) -> dict[str, Any]:
    """Erase the identity, preserve the evidence (module docstring)."""
    from django.apps import apps as django_apps
    from apps.moderation.services import apply_removal
    from apps.profiles.models import Profile

    now = now or timezone.now()
    # Reload inside the transaction (fresh truth, not a stale view copy).
    fresh = get_user_model().objects.get(pk=user.pk)
    if is_anonymized(fresh):
        raise AlreadyAnonymized(
            "Account is already anonymized — refusing (idempotence)."
        )

    counts: dict[str, Any] = {}
    counts["old_username"] = fresh.username
    counts["old_email"] = fresh.email
    counts["old_phone"] = fresh.phone

    # 1) Content: remove through moderation's public door (rows survive,
    #    status REMOVED; the search triggers rebuild their docs).
    for model_ref, field, label in _REMOVAL_TARGETS:
        model = django_apps.get_model(model_ref)
        removed = 0
        for obj in model.objects.filter(**{field: user}).only("id", "status"):
            # NOTE: apply_removal flips status -> REMOVED (evidence kept);
            # it's the SAME gate a moderator uses — no special deletion path.
            if apply_removal(obj):
                removed += 1
        counts[label] = removed

    # 2) Identity erasure in place (the row survives for FK resolvability).
    fresh.username = anon_code_for(fresh)
    fresh.first_name = ""
    fresh.last_name = ""
    fresh.email = ""           # email is not unique (AbstractUser) — safe to blank
    fresh.phone = ""
    fresh.is_active = False    # deactivation: no re-login behind erased identity
    fresh.email_verified_at = None
    fresh.phone_verified_at = None
    fresh.set_unusable_password()
    fresh.save(update_fields=[
        "username", "first_name", "last_name", "email", "phone", "is_active",
        "email_verified_at", "phone_verified_at", "password",
    ])

    # 3) Profile identity text blanked (KDPA minimization post-erasure).
    profile, _created = Profile.objects.get_or_create(user=fresh)
    profile.display_name = ""
    profile.bio = ""
    profile.ward = ""
    profile.skills = []
    profile.interests = []
    profile.precise_location_consent = False
    profile.avatar = None
    profile.save(update_fields=[
        "display_name", "bio", "ward", "skills", "interests",
        "precise_location_consent", "avatar",
    ])

    # 4) Live sessions killed (deactivation alone leaves an open browser).
    counts["sessions_killed"] = _kill_sessions(fresh.pk, now)

    # 5) NOT touched (intentional, tests pin): moderation Reports, audit
    #    rows (moderation/fleet), consent events, disputes/reviews/barter
    #    rows, messaging bodies, guide assets — the documented trade-offs.

    # The erasure REASON for the DSAR ledger is supplied by the fulfilling
    # flow (fulfill_deletion_request); here we only return the facts.
    self_reason = reason.strip()
    if self_reason:
        counts["erasure_reason"] = self_reason
    return counts


# ------------------------------------------------------------ DSAR machine
def open_data_request(user, *, kind: str, note: str = "",
                      now: datetime | None = None) -> DataRequest:
    """The member asks for ACCESS / DELETE / CORRECT (the ledger's first row).

    One OPEN DELETE per member: an erasure already in flight is shown as
    pending instead of queueing duplicates (refused/fulfilled ones do not
    block a fresh ask — e.g. a refusal the member escalates).
    """
    now = now or timezone.now()
    existing = DataRequest.objects.filter(
        user=user, kind=kind, status=RequestStatus.OPEN.value,
    )
    if existing.exists():
        raise ValidationError(
            {"kind": _("A %s request is already open for this account.") % kind}
        )
    # Anonymized accounts cannot open requests: there is no personal data
    # left to access (and no live login, but a shell/admin path may still
    # try — refuse honestly rather than silently queue paperwork).
    if getattr(user, "pk", None) and is_anonymized(user):
        raise ValidationError(_("This account is anonymized; nothing to request."))
    req = DataRequest(
        user=user, kind=kind, note=(note or "").strip(),
        status=RequestStatus.OPEN.value,
    )
    req.full_clean()  # status is OPEN + handled_at None: model clean passes
    req.save()
    return req


def _assert_open(req: DataRequest, target: str) -> None:
    if req.status != RequestStatus.OPEN.value:
        raise ValidationError(
            {target: _("Only an OPEN request can be handled.")}
        )


@transaction.atomic
def fulfill_request(req, *, by, now: datetime | None = None,
                    note: str = "") -> DataRequest:
    """OPEN -> FULFILLED, stamped with the staff member + moment."""
    now = now or timezone.now()
    _assert_open(req, "status")
    req.status = RequestStatus.FULFILLED.value
    req.handled_by = by
    req.handled_at = now
    if note:
        req.note = note
    req.full_clean()
    req.save()
    return req


@transaction.atomic
def refuse_request(req, *, by, now: datetime | None = None,
                   note: str = "") -> DataRequest:
    """OPEN -> REFUSED; the REFUSAL REASON is mandatory (the ODPC file is
    useless without the why)."""
    now = now or timezone.now()
    if not (note or "").strip():
        raise ValidationError({"note": _("A refusal needs a written reason.")})
    _assert_open(req, "status")
    req.status = RequestStatus.REFUSED.value
    req.handled_by = by
    req.handled_at = now
    req.note = note.strip()
    req.full_clean()
    req.save()
    return req


def fulfill_deletion_request(req, *, by, now: datetime | None = None,
                             note: str = "") -> DataRequest:
    """The DELETE fulfilment: run anonymize_account, then close the request
    with an honest note of what was erased and what stands (the DPA
    balance, written where the ODPC auditor reads the ledger)."""
    now = now or timezone.now()
    try:
        counts = anonymize_account(req.user, now=now, reason=req.note)
    except AlreadyAnonymized:
        # Request opened AFTER the erase already happened (e.g. a duplicate
        # ask from the staff search): the decided outcome still gets its row.
        counts = {"already_anonymized": True}
    summary = ", ".join(
        f"{k}={v}" for k, v in sorted(counts.items())
        if not str(k).startswith("old_")
    )
    return fulfill_request(req, by=by, now=now,
                           note=(note or "").strip() or
                                ("erasure applied: " + summary))
