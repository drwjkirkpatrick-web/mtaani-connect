"""lang_chips: the one-line template integration for mixed-feed chips.

Parent wiring seam (12-seam) — in a feed/messaging/list template:

    {% load lang_chips %}
    {% lang_chip item %}     {# renders <span class="lang chip-sw">…</span> #}

or, to keep the DOM yourself:

    {% get_lang_chip item as chip %}<span class="{{ chip.css }}">{{ chip.label }}</span>
"""
from django import template
from django.utils.safestring import mark_safe

from apps.lang.chips import language_chip

register = template.Library()

_LABEL_SAFE = {"sw": "Kiswahili", "en": "English", "sheng": "Sheng", "": "content"}


@register.simple_tag(takes_context=True)
def get_lang_chip(context, obj):
    """Expose the pure chip dict (no auto-escaping concerns for templates)."""
    return language_chip(obj)


@register.simple_tag(takes_context=True)
def lang_chip(context, obj):
    """Render the chip markup directly."""
    chip = language_chip(obj)
    label = _LABEL_SAFE.get(chip["label"], chip["label"])
    return mark_safe(
        f'<span class="{chip["css"]}">{label}</span>'  # noqa: S308 — fixed vocabulary
    )
