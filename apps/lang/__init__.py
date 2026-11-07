"""apps.lang — content-language preference (PROMPTS.md Prompt 35).

WHY a separate app (one-app-per-prompt rule): the CHROME language (en/sw,
Django's locale machinery, Prompt 05) is different from the CONTENT language
(what members wrote). This app owns only the reading preference + the chip
display contract; content models stay untouched (they are other prompts'
files). See apps/core/i18n.py for the chrome drift detector.
"""
