"""Post form (Prompt 10) — plain text + educational tag + typed guide ref.

NOTE: there is deliberately NO url/HTML field. Typed references only
(PROMPTS.md pitfall): a raw URL field would be both an XSS surface and an
unpreviewable blob — previews must render server-side from typed links.
ref_listing also gets no form field yet: Prompt 17 (listings) does not exist,
so collecting listing ids today would be data we cannot honestly preview
(KDPA minimal data + no-fabrication rules).
"""
from django import forms
from django.utils.translation import gettext_lazy as _

from apps.guides.services import visible_guides
from apps.media.models import MediaRef

from .models import MAX_POST_CHARS, Post


class PostForm(forms.Form):
    text = forms.CharField(
        label=_("Post"),
        max_length=MAX_POST_CHARS,  # server-side; maxlength attr is decoration
        widget=forms.Textarea(
            attrs={
                "maxlength": MAX_POST_CHARS,
                "rows": 4,
                "placeholder": _("Share what's happening — short is perfect"),
            }
        ),
    )
    educational = forms.BooleanField(
        label=_("Educational content"),
        required=False,
        help_text=_(
            "Tick when this teaches something useful to the neighbourhood."
        ),
    )
    ref_guide = forms.ModelChoiceField(
        label=_("Link a guide"),
        queryset=MediaRef.objects.none(),  # replaced per-viewer in __init__
        required=False,
        empty_label=_("No guide linked"),
    )

    def __init__(self, *args, viewer=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Only guides THIS viewer can see are linkable (their own drafts +
        # everything published) — the queryset comes from the guides app's
        # public query function, never its tables.
        self.fields["ref_guide"].queryset = visible_guides(viewer)
