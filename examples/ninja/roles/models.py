import uuid

from django.db import models
from django.utils import timezone


def new_etag() -> str:
    """A fresh entity-tag value (a new one is set on every write)."""
    return uuid.uuid4().hex


class Role(models.Model):
    """A Role-like resource, scoped by ``rbac_context`` as with scoped-rbac.

    ``etag`` changes on every write and is the ETag; ``modified_at`` is the
    Last-Modified time. The writes in ``roles/api.py`` set both.
    """

    name = models.CharField(max_length=100)
    rbac_context = models.CharField(max_length=100, db_index=True)
    etag = models.CharField(max_length=32, default=new_etag, editable=False)
    modified_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        # The list is paginated: an unordered queryset would make its pages
        # unstable (and raise Django's UnorderedObjectListWarning).
        ordering = ["pk"]

    def __str__(self) -> str:
        return self.name
