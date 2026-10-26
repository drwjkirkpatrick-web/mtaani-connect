"""Thin views (PROMPTS.md Prompt 04): validate, delegate to services, redirect.

WHY thin: flows and rules are testable in services without HTTP; views only
wire request data + adapters from settings.
"""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.module_loading import import_string
from django.views.decorators.http import require_http_methods

from apps.core.comms import EmailAdapter
from .forms import RecoverRequestForm, RecoverResetForm, SignupForm, VerifyForm
from .models import CodePurpose
from .services import (
    RateLimited, login_throttled, record_login_attempt, redeem_recovery,
    request_recovery, send_verification_code, signup, verify_code,
)


def _email_adapter() -> EmailAdapter:
    # NOTE: settings carries dotted paths so real adapters swap via env only.
    return import_string(settings.EMAIL_ADAPTER)()


def _sms_adapter():
    return import_string(settings.SMS_ADAPTER)()


def _ip(request) -> str | None:
    return request.META.get("REMOTE_ADDR") or None


@require_http_methods(["GET", "POST"])
def signup_view(request):
    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            try:
                user = signup(
                    email=form.cleaned_data["email"],
                    password=form.cleaned_data["password"],
                    phone=form.cleaned_data["phone"],
                    ip=_ip(request),
                    now=timezone.now(),
                    email_adapter=_email_adapter(),
                )
            except RateLimited:
                form.add_error(None, "Too many codes requested. Try again in an hour.")
            else:
                login(request, user)
                messages.info(request, "Check your email for the verification code.")
                return redirect("accounts:verify_email")
    else:
        form = SignupForm()
    return render(request, "accounts/signup.html", {"form": form})


@require_http_methods(["GET", "POST"])
def verify_email_view(request):
    if request.method == "POST":
        form = VerifyForm(request.POST)
        if form.is_valid():
            ok = verify_code(
                target=request.user.email,
                purpose=CodePurpose.VERIFY_EMAIL,
                code=form.cleaned_data["code"],
                now=timezone.now(),
            )
            if ok:
                request.user.email_verified_at = timezone.now()
                request.user.save(update_fields=["email_verified_at"])
                messages.success(request, "Email verified. Asante!")
                return redirect("home")
            form.add_error(None, "That code is wrong or expired.")
    else:
        form = VerifyForm()
    return render(request, "accounts/verify.html", {"form": form, "kind": "email"})


@require_http_methods(["GET", "POST"])
def verify_phone_view(request):
    if request.method == "POST":
        form = VerifyForm(request.POST)
        if form.is_valid():
            ok = verify_code(
                target=request.user.phone,
                purpose=CodePurpose.VERIFY_PHONE,
                code=form.cleaned_data["code"],
                now=timezone.now(),
            )
            if ok:
                request.user.phone_verified_at = timezone.now()
                request.user.save(update_fields=["phone_verified_at"])
                messages.success(request, "Phone verified. Asante!")
                return redirect("home")
            form.add_error(None, "That code is wrong or expired.")
    else:
        if request.user.phone:
            send_verification_code(
                target=request.user.phone,
                channel="SMS",
                purpose=CodePurpose.VERIFY_PHONE,
                now=timezone.now(),
                adapter=_sms_adapter(),
            )
        form = VerifyForm()
    return render(request, "accounts/verify.html", {"form": form, "kind": "phone"})


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.method == "POST":
        username = request.POST.get("username", "")
        password = request.POST.get("password", "")
        ip = _ip(request)
        if login_throttled(username=username, ip=ip, now=timezone.now()):
            messages.error(request, "Too many attempts. Try again in 15 minutes.")
            return render(request, "accounts/login.html")
        user = authenticate(request, username=username, password=password)
        if user is not None:
            record_login_attempt(username=username, ip=ip, succeeded=True, now=timezone.now())
            login(request, user)
            return redirect("home")
        record_login_attempt(username=username, ip=ip, succeeded=False, now=timezone.now())
        messages.error(request, "Wrong username or password.")
    return render(request, "accounts/login.html")


@require_http_methods(["GET", "POST"])
def recover_request_view(request):
    if request.method == "POST":
        form = RecoverRequestForm(request.POST)
        if form.is_valid():
            # Anti-enumeration: same outcome message whether or not the email exists.
            request_recovery(
                email=form.cleaned_data["email"],
                now=timezone.now(),
                email_adapter=_email_adapter(),
            )
            messages.info(request, "If that email is registered, a reset code is on its way.")
            return redirect("accounts:recover_reset")
    else:
        form = RecoverRequestForm()
    return render(request, "accounts/recover_request.html", {"form": form})


@require_http_methods(["GET", "POST"])
def recover_reset_view(request):
    if request.method == "POST":
        form = RecoverResetForm(request.POST)
        if form.is_valid():
            ok = redeem_recovery(
                token=form.cleaned_data["token"],
                new_password=form.cleaned_data["new_password"],
                now=timezone.now(),
            )
            if ok:
                messages.success(request, "Password set. Karibu tena.")
                return redirect("accounts:login")
            form.add_error(None, "That code is wrong or expired.")
    else:
        initial = {"token": request.GET.get("token", "")}
        form = RecoverResetForm(initial=initial)
    return render(request, "accounts/recover_reset.html", {"form": form})
