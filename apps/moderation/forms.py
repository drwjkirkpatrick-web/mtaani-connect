"""Report form (Prompt 15).

NOTE: this form is the UI face of report_create; the allow-list is ALSO
enforced on the model (Report.clean) and in the service, so script/admin/API
callers can never bypass it. content_type choices are restricted to the
allowed app labels, object_id must resolve to a live object.
"""
from django import forms
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.core.types import ReportReason

from .models import ALLOWED_TARGET_APPS, MAX_NOTE_CHARS


class ReportForm(forms.Form):
    content_type = forms.ModelChoiceField(
        queryset=ContentType.objects.filter(app_label__in=ALLOWED_TARGET_APPS),
        label=_("What should be reviewed?"),
        empty_label=_("Choose content type"),
    )
    object_id = forms.IntegerField(label=_("Content ID"))
    reason = forms.ChoiceField(choices=ReportReason.choices, label=_("Reason"))
    note = forms.CharField(
        label=_("Details (optional)"),
        max_length=MAX_NOTE_CHARS,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Up to 500 characters."),
    )
    next = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.target = None

    def clean(self):
        cleaned = super().clean()
        content_type = cleaned.get("content_type")
        object_id = cleaned.get("object_id")
        if content_type is not None and object_id is not None:
            model_class = content_type.model_class()
            if model_class is None:
                raise ValidationError(_("Unknown content type."))
            target = model_class.objects.filter(pk=object_id).first()
            if target is None:
                raise ValidationError(_("That content could not be found."))
            self.target = target
            cleaned["target"] = target
        return cleaned
