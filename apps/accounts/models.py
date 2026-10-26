"""Custom user model — Prompt 01 skeleton (Prompt 04 owns everything else).

WHY this exists in Prompt 01 and not 04: Django requires AUTH_USER_MODEL to
be final BEFORE the first migrate. Swapping it once the auth tables exist is
effectively unrecoverable, so the custom user lands with the scaffold and
Prompt 04 builds verification/recovery on top of it.
"""
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Mtaani Connect user.

    NOTE: `phone` is optional and stored E.164 (+254...) when present.
    Prompt 04 adds verification flows; nothing in V1 requires a phone.
    """

    phone = models.CharField(max_length=16, blank=True, default="", db_index=True)
