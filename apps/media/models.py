"""MediaRef — the one persisted record per uploaded image (Prompt 06).

WHY paths not blobs: the DB stores metadata; files live under MEDIA_ROOT —
serving files from a DB is how bundles get eaten (hard rule: budgets).
"""
import uuid

from django.conf import settings
from django.db import models

from apps.core.types import TimestampedModel


class MediaRef(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="media"
    )
    purpose = models.CharField(max_length=32, db_index=True)

    standard_path = models.CharField(max_length=200)
    standard_bytes = models.PositiveIntegerField()
    standard_w = models.PositiveSmallIntegerField()
    standard_h = models.PositiveSmallIntegerField()

    thumb_path = models.CharField(max_length=200)
    thumb_bytes = models.PositiveIntegerField()
    thumb_w = models.PositiveSmallIntegerField()
    thumb_h = models.PositiveSmallIntegerField()

    original_bytes = models.PositiveIntegerField()
    original_format = models.CharField(max_length=8)

    def standard_file(self):
        from pathlib import Path

        return Path(settings.MEDIA_ROOT) / self.standard_path

    def thumb_file(self):
        from pathlib import Path

        return Path(settings.MEDIA_ROOT) / self.thumb_path
