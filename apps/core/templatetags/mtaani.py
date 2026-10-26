"""Mtaani template tags (Prompt 05). Owns the language switch component."""
from django import template

register = template.Library()


@register.inclusion_tag("core/_lang_switch.html", takes_context=True)
def lang_switch(context):
    """Render the EN | SW toggle, remembering the current path."""
    return {
        "languages": [("en", "EN"), ("sw", "SW")],
        "current": context.get("LANGUAGE_CODE", "en"),
        "next": context["request"].path,
    }
