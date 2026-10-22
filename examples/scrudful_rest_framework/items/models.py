from django.db import models


class Item(models.Model):
    """A resource with the two validators scrudful needs.

    ``version`` is incremented on every save and is the basis of the ETag;
    ``updated_at`` is the Last-Modified time.
    """

    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pk"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self._state.adding:
            self.version += 1
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "version", "updated_at"}
        super().save(*args, **kwargs)
