"""Prompt 04 acceptance: comms adapters are offline, deterministic, leak-free."""
from apps.core.comms import DeliveryResult, MockEmail, MockSMS, deterministic_code


def test_mock_sms_records_sends():
    sms = MockSMS()
    result = sms.send("+254712345678", "Mtaani code: 123456")
    assert result.ok and result.channel == "sms"
    assert sms.sent == [("+254712345678", "Mtaani code: 123456")]


def test_mock_email_records_sends():
    mail = MockEmail()
    result = mail.send("a@b.co", "hi", "body")
    assert result.ok and result.channel == "email"
    assert mail.sent == [("a@b.co", "hi", "body")]


def test_delivery_result_has_no_message_field():
    """HARD RULE: results/logs must never carry OTP bodies."""
    import dataclasses
    assert "message" not in {f.name for f in dataclasses.fields(DeliveryResult)}


def test_deterministic_code_is_stable_and_six_digits():
    a = deterministic_code("+254712345678", "2026-01-15")
    b = deterministic_code("+254712345678", "2026-01-15")
    c = deterministic_code("+254712345678", "2026-01-16")
    assert a == b and len(a) == 6 and a.isdigit()
    assert a != c  # day rotation changes the code
