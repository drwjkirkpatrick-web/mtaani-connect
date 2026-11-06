"""Forms for Prompt 20 — plain server-rendered (no JS contract in V1).

Body is plain text (≤2000 chars — the cap lives on the MODEL too, so no write
path skips it). Photos ride in from request.FILES and are capped at 4 per
message in services.send_message (service-side reason + model-side slot cap).

The report form's message picker is built view-side (choices = the currently
rendered message window of THIS thread) — a compact way to say "which message"
without fifty per-message forms (page budget).
"""
from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.types import ReportReason

from .models import MAX_BODY_CHARS


class MessageForm(forms.Form):
    """The send form on the thread detail page."""

    body = forms.CharField(
        label=_("Message"),
        max_length=MAX_BODY_CHARS,
        required=False,  # a photo-only message is legal; empty+photoless is not
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "maxlength": MAX_BODY_CHARS,
                "placeholder": _("Write a message…"),
                "cols": 20,
            }
        ),
    )


class ReportMessageForm(forms.Form):
    """'Report message → Prompt 15 queue' (routing contract in Prompt 20)."""

    def __init__(self, *args, messages_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["message_pk"] = forms.ChoiceField(
            label=_("Message to report"),
            choices=messages_choices or [],
        )

    reason = forms.ChoiceField(
        choices=ReportReason.choices,
        label=_("Reason"),
        initial=ReportReason.SPAM.value,
    )
    note = forms.CharField(
        label=_("Note"),
        max_length=500,  # moderation's Report.note cap mirrored here for UX
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
