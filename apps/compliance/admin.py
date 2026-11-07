"""Admin for the compliance registry + DSAR queue (Prompt 40).

Read/operate conveniences for staff only — the views are the main doors;
the admin exists so fixing a malformed inventory row or checking a
request's stamps is possible without SQL. Nothing here accepts user data
input beyond the request workflow itself.
"""
from django.contrib import admin

from .models import DataMapEntry, DataRequest


@admin.register(DataMapEntry)
class DataMapEntryAdmin(admin.ModelAdmin):
    list_display = ("app_label", "model_name", "purpose", "lawful_basis",
                    "retention_days", "updated_at")
    list_filter = ("lawful_basis", "app_label")
    search_fields = ("app_label", "model_name", "purpose")

    # WHY save_model: keep editing possible (ops fixes) but never silently —
    # the loader's next upsert re-normalizes rows to the hand inventory.
    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DataRequest)
class DataRequestAdmin(admin.ModelAdmin):
    list_display = ("user", "kind", "status", "handled_by", "handled_at")
    list_filter = ("kind", "status")
    # raw_id (not autocomplete): no Django-admin exists for the custom User
    # (Prompt 01) — autocompleting it would demand admin.E039 registration.
    raw_id_fields = ("user", "handled_by")
    # NOTE: state transitions belong to the services (state machine +
    # side effects like anonymization); the admin surface is read-mostly.
    def has_change_permission(self, request, obj=None):
        return False
    def has_add_permission(self, request):
        return False
