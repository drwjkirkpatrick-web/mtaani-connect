"""Accounts models (PROMPTS.md Prompt 04).

The custom User skeleton landed in Prompt 01 (the AUTH_USER_MODEL trap).
Prompt 04 owns everything else here: verification codes, recovery tokens,
login-attempt bookkeeping, and KDPA consent events.
"""
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Mtaani Connect user.

    NOTE: `phone` is optional and stored E.164 (+254...) when present — the
    forms layer enforces the format before anything else touches it.
    """

    phone = models.CharField(max_length=16, blank=True, default="", db_index=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    phone_verified_at = models.DateTimeField(null=True, blank=True)


class Channel(models.TextChoices):
    EMAIL = "EMAIL", "Email"
    SMS = "SMS", "SMS"


class CodePurpose(models.TextChoices):
    VERIFY_EMAIL = "VERIFY_EMAIL", "Verify email"
    VERIFY_PHONE = "VERIFY_PHONE", "Verify phone"
    RECOVER = "RECOVER", "Account recovery"


class ConsentEvent(models.Model):
    """KDPA §32: the controller bears the burden of proving consent, so the
    record must be reconstructable — who, which document version, when, from
    where. Never hard-delete these (moderation evidence retention, Prompt 40).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="consent_events"
    )
    purpose_key = models.CharField(max_length=64)  # "terms" | "privacy"
    doc_version = models.CharField(max_length=32)  # e.g. "2026-10-25.1"
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "purpose_key", "doc_version"],
                name="uniq_consent_user_purpose_version",
            )
        ]


class VerificationCode(models.Model):
    """An OTP: hashed at rest, single-use, expiring.

    NOTE: created_at is set explicitly by the service (injected clock), so
    auto_now_add is deliberately NOT used here (PROMPTS.md: injectable clocks).
    """

    target = models.CharField(max_length=254)  # email address or E.164 phone
    channel = models.CharField(max_length=8, choices=Channel.choices)
    purpose = models.CharField(max_length=32, choices=CodePurpose.choices)
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)


class RecoveryToken(models.Model):
    """Account recovery: hashed, single-use, 1-hour expiry (hard rule)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recovery_tokens"
    )
    token_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)


class LoginAttempt(models.Model):
    """Login throttle bookkeeping: 5 failures -> 15-minute lock per user+IP.

    NOTE: username is stored normalised (lowercase); IP kept for the lock
    scope only — KDPA minimization: no user-agent, no device fingerprinting.
    """

    username_norm = models.CharField(max_length=150, db_index=True)
    ip = models.GenericIPAddressField(null=True)
    succeeded = models.BooleanField(default=False)
    created_at = models.DateTimeField()
