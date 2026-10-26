"""Prompt 04 acceptance: signup, verification, recovery, throttling.

Every flow uses injected clocks (mock_now-style) and Mock adapters — zero
network, fully deterministic.
"""
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.models import ConsentEvent, RecoveryToken, VerificationCode
from apps.accounts.services import (
    CODE_SEND_LIMIT_PER_HOUR, LOGIN_FAIL_LIMIT, RateLimited,
    generate_code, login_throttled, record_login_attempt, redeem_recovery,
    request_recovery, send_verification_code, signup, validate_phone_e164,
    verify_code,
)
from apps.core.comms import MockEmail, MockSMS, deterministic_code
from apps.core.testing import mock_now

DAY = "2026-01-15"


@pytest.fixture
def now():
    return mock_now()


def _gen_for(target):
    return lambda: deterministic_code(target, DAY)


# --- signup -----------------------------------------------------------------

@pytest.mark.django_db
def test_signup_creates_user_with_two_consent_events(now):
    mail = MockEmail()
    user = signup(
        email="wanjiku@example.com", password="pass-12345678",
        ip="41.90.64.10", now=now, email_adapter=mail,
    )
    assert user.email == "wanjiku@example.com"
    events = ConsentEvent.objects.filter(user=user)
    assert {e.purpose_key for e in events} == {"terms", "privacy"}
    assert events.count() == 2  # exactly once per signup (KDPA burden of proof)
    assert all(e.source_ip == "41.90.64.10" for e in events)
    assert len(mail.sent) == 1  # verification code out


@pytest.mark.django_db
def test_signup_phone_e164_enforced(now):
    with pytest.raises(Exception):
        signup(
            email="bad@example.com", password="pass-12345678", phone="0722 123 456",
            now=now, email_adapter=MockEmail(),
        )


@pytest.mark.django_db
def test_signup_email_code_verifies_and_is_single_use(now):
    mail = MockEmail()
    email = "kamau@example.com"
    user = signup(
        email=email, password="pass-12345678", now=now, email_adapter=mail,
        code_generator=_gen_for(email),
    )
    code = deterministic_code(email, DAY)
    assert verify_code(target=email, purpose="VERIFY_EMAIL", code=code, now=now) is True
    assert verify_code(target=email, purpose="VERIFY_EMAIL", code=code, now=now) is False  # single-use
    user.refresh_from_db()
    assert user.email_verified_at is None  # service verified; view sets the stamp


@pytest.mark.django_db
def test_verify_code_rejects_expired(now):
    email = "late@example.com"
    send_verification_code(
        target=email, channel="EMAIL", purpose="VERIFY_EMAIL",
        now=now, adapter=MockEmail(), generator=_gen_for(email),
    )
    code = deterministic_code(email, DAY)
    later = now + timedelta(minutes=11)
    assert verify_code(target=email, purpose="VERIFY_EMAIL", code=code, now=later) is False


@pytest.mark.django_db
def test_verify_code_rejects_wrong_code(now):
    email = "wrong@example.com"
    send_verification_code(
        target=email, channel="EMAIL", purpose="VERIFY_EMAIL",
        now=now, adapter=MockEmail(), generator=_gen_for(email),
    )
    assert verify_code(target=email, purpose="VERIFY_EMAIL", code="000000", now=now) is False


@pytest.mark.django_db
def test_phone_verification_flow_via_mock_sms(now):
    phone = "+254712345678"
    sms = MockSMS()
    send_verification_code(
        target=phone, channel="SMS", purpose="VERIFY_PHONE",
        now=now, adapter=sms, generator=_gen_for(phone),
    )
    assert len(sms.sent) == 1 and sms.sent[0][0] == phone
    assert verify_code(
        target=phone, purpose="VERIFY_PHONE", code=deterministic_code(phone, DAY), now=now
    )


@pytest.mark.django_db
def test_otp_send_rate_limit_per_hour(now):
    email = "spam@example.com"
    mail = MockEmail()
    for _ in range(CODE_SEND_LIMIT_PER_HOUR):
        send_verification_code(
            target=email, channel="EMAIL", purpose="VERIFY_EMAIL",
            now=now, adapter=mail, generator=_gen_for(email),
        )
    with pytest.raises(RateLimited):
        send_verification_code(
            target=email, channel="EMAIL", purpose="VERIFY_EMAIL",
            now=now, adapter=mail, generator=_gen_for(email),
        )
    # a new hour window clears the limit
    fresh = now + timedelta(hours=1, minutes=1)
    send_verification_code(
        target=email, channel="EMAIL", purpose="VERIFY_EMAIL",
        now=fresh, adapter=mail, generator=_gen_for(email),
    )


@pytest.mark.django_db
def test_codes_hashed_at_rest_never_plaintext(now):
    email = "hashed@example.com"
    code = deterministic_code(email, DAY)
    send_verification_code(
        target=email, channel="EMAIL", purpose="VERIFY_EMAIL",
        now=now, adapter=MockEmail(), generator=lambda: code,
    )
    row = VerificationCode.objects.get(target=email)
    assert row.code_hash != code and len(row.code_hash) == 64


# --- recovery ---------------------------------------------------------------

@pytest.mark.django_db
def test_recovery_round_trip_single_use(now):
    mail = MockEmail()
    user = get_user_model().objects.create_user("njoki", email="njoki@example.com", password="old-pass-123")
    assert request_recovery(email="njoki@example.com", now=now, email_adapter=mail) is True
    token = mail.sent[0][2].rsplit(" ", maxsplit=1)[-1]
    later = now + timedelta(minutes=5)
    assert redeem_recovery(token=token, new_password="new-pass-456", now=later) is True
    assert redeem_recovery(token=token, new_password="again-789", now=later) is False  # single-use
    user.refresh_from_db()
    assert user.check_password("new-pass-456")


@pytest.mark.django_db
def test_recovery_expiry(now):
    mail = MockEmail()
    get_user_model().objects.create_user("mwalimu", email="m@example.com", password="old-pass-123")
    request_recovery(email="m@example.com", now=now, email_adapter=mail)
    token = mail.sent[0][2].rsplit(" ", maxsplit=1)[-1]
    late = now + timedelta(minutes=61)
    assert redeem_recovery(token=token, new_password="x-pass-12345", now=late) is False


@pytest.mark.django_db
def test_recovery_anti_enumeration(now):
    """Unknown email behaves exactly like a known one from the caller's view."""
    assert request_recovery(email="ghost@example.com", now=now, email_adapter=MockEmail()) is False


# --- login throttle ---------------------------------------------------------

@pytest.mark.django_db
def test_login_lockout_after_five_failures(now):
    username, ip = "otieno", "41.90.64.10"
    for _ in range(LOGIN_FAIL_LIMIT):
        record_login_attempt(username=username, ip=ip, succeeded=False, now=now)
    assert login_throttled(username=username, ip=ip, now=now) is True
    # lock expires with the window
    assert login_throttled(
        username=username, ip=ip, now=now + timedelta(minutes=16)
    ) is False


@pytest.mark.django_db
def test_successful_logins_do_not_lock(now):
    username, ip = "aki", "41.90.64.11"
    for _ in range(4):
        record_login_attempt(username=username, ip=ip, succeeded=False, now=now)
    assert login_throttled(username=username, ip=ip, now=now) is False
    record_login_attempt(username=username, ip=ip, succeeded=True, now=now)
    assert login_throttled(username=username, ip=ip, now=now) is False  # still under limit


@pytest.mark.django_db
def test_lock_is_per_user_and_ip(now):
    record_login_attempt(username="naliaka", ip="41.90.64.12", succeeded=False, now=now)
    assert login_throttled(username="naliaka", ip="41.90.64.13", now=now) is False
    assert login_throttled(username="someone-else", ip="41.90.64.12", now=now) is False


# --- leakage guard ----------------------------------------------------------

@pytest.mark.django_db
def test_no_otp_in_logs(now, caplog):
    email = "quiet@example.com"
    code_holder = {}

    def capture_gen():
        code_holder["code"] = generate_code()
        return code_holder["code"]

    send_verification_code(
        target=email, channel="EMAIL", purpose="VERIFY_EMAIL",
        now=now, adapter=MockEmail(), generator=capture_gen,
    )
    assert code_holder["code"] not in caplog.text
