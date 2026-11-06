"""Q&A forms (Prompt 12).

IS_MACHINE GUARD (the core pitfall): no form here includes `is_machine` —
a form-driven Answer instance can never carry the AI label. Adding extra
POST keys does nothing: Django forms ignore fields outside Meta.fields.
The label travels through apps.qa.services.set_machine_answer only.
"""
from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.types import SafetyCategory

from .models import Answer, Question, MAX_BODY_CHARS


class QuestionForm(forms.ModelForm):
    class Meta:
        model = Question
        fields = ("title_en", "title_sw", "body", "safety_category")
        widgets = {
            "body": forms.Textarea(attrs={"rows": 4}),
        }
        # NOTE: safety_category IS user-choosable here — the asker declares
        # the topic (medical/electrical/construction), which is what makes
        # answers require warnings and the accept gate apply at all.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title_sw"].required = False
        self.fields["body"].required = False
        self.fields["safety_category"].choices = SafetyCategory.choices


class AnswerForm(forms.ModelForm):
    # WHY body + safety_warning only: the answer's safety category is the
    # question's flag (single source of truth), and is_machine is NEVER
    # form-editable (see module docstring).
    class Meta:
        model = Answer
        fields = ("body", "safety_warning")
        widgets = {
            "body": forms.Textarea(attrs={"rows": 4}),
            "safety_warning": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["safety_warning"].required = False  # enforced by the model
        self.fields["body"].max_length = MAX_BODY_CHARS
