"""Account routes (PROMPTS.md Prompt 04)."""
from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("signup/", views.signup_view, name="signup"),
    path("login/", views.login_view, name="login"),
    path("verify-email/", views.verify_email_view, name="verify_email"),
    path("verify-phone/", views.verify_phone_view, name="verify_phone"),
    path("recover/", views.recover_request_view, name="recover_request"),
    path("recover/reset/", views.recover_reset_view, name="recover_reset"),
]
