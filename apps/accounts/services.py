"""Auth flows (PROMPTS.md Prompt 04): signup, verification, recovery, throttling.

Every function takes an explicit `now` (injected clock — ranking/decay/expiry
logic must be testable) and an explicit adapter (mock-first hard rule).
"""
from __future__ import annotations

import hmac
import re
import secrets
import hashlib
from datetime import timedelta
from typing import Callable

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.core.comms import EmailAdapter, SMSAdapter
from .models import CodePurpose, ConsentEvent, LoginAttempt, RecoveryToken, VerificationCode

# ---- tunables (structure, not secrets) -------------------------------------
CODE_EXPIRY_MIN = 10
CODE_SEND_LIMIT_PER_HOUR = 3
LOGIN_FAIL_LIMIT = 5
LOGIN_LOCK_MIN = 15
RECOVERY_EXPIRY_MIN = 60
TERMS_VERSION = "2026-10-25.1"
PRIVACY_VERSION = "2026-10-25.1"

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


class RateLimited(Exception):
    """Raised when a target exceeds CODE_SEND_LIMIT_PER_HOUR sends."""


def _hash(value: str) -> str:
    """HMAC-SHA256 keyed by SECRET_KEY — hashed at rest, never plaintext."""
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def generate_code() -> str:
    """Random 6-digit OTP (production path). Tests inject deterministic ones."""
    return f"{secrets.randbelow(1_000_000):06d}"


def validate_phone_e164(phone: str) -> None:
    """E.164 or reject — the forms call this before anything else sees it."""
    if not phone or not E164_RE.match(phone):
        raise ValidationError("Phone must be in international format, e.g. +254712345678.")


def send_verification_code(
    *,
    target: str,
    channel: str,
    purpose: str,
    now,
    adapter: SMSAdapter | EmailAdapter,
    generator: Callable[[], str] | None = None,
) -> VerificationCode:
    """Create + 'send' one OTP. Rate-limited per (target, purpose) per hour."""
    window_start = now - timedelta(hours=1)
    recent = VerificationCode.objects.filter(
        target=target, purpose=purpose, created_at__gte=window_start
    ).count()
    if recent >= CODE_SEND_LIMIT_PER_HOUR:
        # WHY refuse loudly here: silent throttles make tests lie.
        raise RateLimited(
            f"Too many codes sent to this {channel}; try again in an hour."
        )
    code = (generator or generate_code)()
    vc = VerificationCode.objects.create(
        target=target,
        channel=channel,
        purpose=purpose,
        code_hash=_hash(code),
        created_at=now,
        expires_at=now + timedelta(minutes=CODE_EXPIRY_MIN),
    )
    if channel == "SMS":
        adapter.send(target, f"Mtaani code: {code}")
    else:
        adapter.send(target, "Your Mtaani code", f"Your verification code is {code}")
    return vc


def verify_code(*, target: str, purpose: str, code: str, now) -> bool:
    """Match + atomically consume one unused, unexpired code (single-use)."""
    updated = VerificationCode.objects.filter(
        target=target,
        purpose=purpose,
        code_hash=_hash(code),
        used_at__isnull=True,
        expires_at__gte=now,
    ).update(used_at=now)
    return updated == 1


def _unique_username(email: str) -> str:
    """Email-first signup: username derived from the email prefix."""
    User = get_user_model()
    base = slugify(email.split("@")[0])[:60] or "user"
    candidate, n = base, 1
    while User.objects.filter(username=candidate).exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def signup(
    *,
    email: str,
    password: str,
    phone: str = "",
    ip: str | None = None,
    now,
    email_adapter: EmailAdapter,
    code_generator: Callable[[], str] | None = None,
) -> "object":
    """Create user + KDPA consent events + send the email verification code.

    Consent events are created EXACTLY once per (user, purpose, version) —
    the unique constraint makes duplicates impossible, not just unlikely.
    """
    validate_email(email)
    if phone:
        validate_phone_e164(phone)
    User = get_user_model()
    user = User.objects.create_user(
        username=_unique_username(email),
        email=email,
        password=password,
        phone=phone,
    )
    for purpose, version in (("terms", TERMS_VERSION), ("privacy", PRIVACY_VERSION)):
        ConsentEvent.objects.create(
            user=user, purpose_key=purpose, doc_version=version,
            source_ip=ip, created_at=now,
        )
    send_verification_code(
        target=email,
        channel="EMAIL",
        purpose=CodePurpose.VERIFY_EMAIL,
        now=now,
        adapter=email_adapter,
        generator=code_generator,
    )
    return user


def request_recovery(
    *, email: str, now, email_adapter: EmailAdapter
) -> bool:
    """Mail a recovery token. Returns False when no such user exists —
    callers ALWAYS show the same generic message (anti-enumeration)."""
    User = get_user_model()
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        return False
    token = secrets.token_urlsafe(32)
    RecoveryToken.objects.create(
        user=user,
        token_hash=_hash(token),
        created_at=now,
        expires_at=now + timedelta(minutes=RECOVERY_EXPIRY_MIN),
    )
    email_adapter.send(
        email, "Reset your Mtaani password",
        f"Use this one-time code within {RECOVERY_EXPIRY_MIN} minutes: {token}",
    )
    return True


def redeem_recovery(*, token: str, new_password: str, now) -> bool:
    """Atomically consume the token and set the new password. Single-use."""
    updated = RecoveryToken.objects.filter(
        token_hash=_hash(token), used_at__isnull=True, expires_at__gte=now
    ).update(used_at=now)
    if updated != 1:
        return False
    row = RecoveryToken.objects.filter(token_hash=_hash(token)).order_by("-created_at").first()
    row.user.set_password(new_password)
    row.user.save(update_fields=["password"])
    return True


def login_throttled(*, username: str, ip: str | None, now) -> bool:
    """True when this (username, ip) hit LOGIN_FAIL_LIMIT failures in the window."""
    failures = LoginAttempt.objects.filter(
        username_norm=username.lower(),
        ip=ip,
        succeeded=False,
        created_at__gte=now - timedelta(minutes=LOGIN_LOCK_MIN),
    ).count()
    return failures >= LOGIN_FAIL_LIMIT


def record_login_attempt(*, username: str, ip: str | None, succeeded: bool, now) -> None:
    LoginAttempt.objects.create(
        username_norm=username.lower(),
        ip=ip,
        succeeded=succeeded,
        created_at=now,
    )
