from django.db import models

from common.models import TimeStampedModel


class Supplier(TimeStampedModel):
    name = models.CharField(max_length=300)
    code = models.CharField(max_length=50, unique=True)
    contact_person = models.CharField(max_length=200, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    website = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
