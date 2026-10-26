"""Forms (PROMPTS.md Prompt 04). Validation lives here; services do the work."""
from django import forms
from django.core.validators import validate_email

from .services import validate_phone_e164


class SignupForm(forms.Form):
    email = forms.EmailField()
    password = forms.CharField(min_length=8, widget=forms.PasswordInput)
    phone = forms.CharField(required=False, help_text="Optional, E.164: +254712345678")

    def clean_phone(self):
        phone = self.cleaned_data.get("phone", "").strip()
        if phone:
            validate_phone_e164(phone)  # raises ValidationError with a friendly message
        return phone


class VerifyForm(forms.Form):
    code = forms.CharField(min_length=6, max_length=6)


class RecoverRequestForm(forms.Form):
    email = forms.EmailField()


class RecoverResetForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)
    new_password = forms.CharField(min_length=8, widget=forms.PasswordInput)
