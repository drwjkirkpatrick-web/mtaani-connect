"""/lang/ routes (Prompt 35) — included by mtaani/urls.py at path 'lang/'.

NOTE: names are global (no namespace) because the parent pre-wired the
include without app_name. They are deliberately prefixed lang_prefs_* so
they cannot collide with Prompt 05's set_lang or any other app's names.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("prefs/set/", views.set_preference, name="lang_prefs_set"),
    path("prefs/status.json", views.status, name="lang_prefs_status"),
    path("prefs/demo/", views.demo, name="lang_prefs_demo"),
]
