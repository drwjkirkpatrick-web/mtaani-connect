"""Communication adapters (PROMPTS.md Prompt 04) — mock-first.

WHY adapters: every SMS/email delivery goes through an interface so tests
run offline and no provider key is ever required (hard rule). Real
adapters (Africa's Talking for SMS, SMTP for email) are documented
extension points — deliberately NOT implemented in V1.

This module is owned by Prompt 04; later prompts (alerts, messaging)
consume these interfaces and never modify them.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one adapter send.

    NOTE: the message body is deliberately NOT part of the result — logs and
    results must never carry OTP bodies (security hard rule, tested).
    """

    ok: bool
    channel: str
    target: str


class SMSAdapter:
    """Interface: deliver a text message to a phone number."""

    def send(self, phone: str, message: str) -> DeliveryResult:  # pragma: no cover
        raise NotImplementedError


class EmailAdapter:
    """Interface: deliver an email."""

    def send(self, email: str, subject: str, message: str) -> DeliveryResult:  # pragma: no cover
        raise NotImplementedError


class MockSMS(SMSAdapter):
    """Offline, deterministic. Records sends for assertions; sends nothing."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, phone: str, message: str) -> DeliveryResult:
        self.sent.append((phone, message))
        return DeliveryResult(ok=True, channel="sms", target=phone)


class MockEmail(EmailAdapter):
    """Offline, deterministic. Records sends for assertions; sends nothing."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, email: str, subject: str, message: str) -> DeliveryResult:
        self.sent.append((email, subject, message))
        return DeliveryResult(ok=True, channel="email", target=email)


def deterministic_code(target: str, day: str) -> str:
    """Stable 6-digit code derived from f(target, day) — tests and demos.

    WHY: tests must drive full OTP flows without reading what a real adapter
    would send. NOTE: production code paths use random generation
    (accounts.services.generate_code) — never this.
    """
    digest = hashlib.sha256(f"mtaani|{target}|{day}".encode("utf-8")).hexdigest()
    return f"{int(digest[:8], 16) % 1_000_000:06d}"
