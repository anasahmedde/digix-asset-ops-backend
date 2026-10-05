from django.db import models

from common.codes import generate_code
from common.models import TimeStampedModel


class Client(TimeStampedModel):
    name = models.CharField(max_length=300)
    code = models.CharField(max_length=50, unique=True, blank=True, db_index=True)
    contact_person = models.CharField(max_length=200, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        # Newest first, like every other register. A catalogue that
        # fills a dropdown stays alphabetical — see PLATFORM_STANDARD §6.4.
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = generate_code("client", model=type(self), field="code")
        super().save(*args, **kwargs)


class ClientContact(TimeStampedModel):
    """Somebody to ring at this client.

    One name and one number on the client itself was never enough — the
    person who signs the order is rarely the person who opens the gate —
    so the people are kept here, each with the job they do. The client's
    own ``contact_*`` fields stay as the primary, written from whichever
    of these is marked primary, so everything already reading them keeps
    working.
    """

    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="contacts",
    )
    name = models.CharField(max_length=200)
    designation = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=20)
    email = models.EmailField(blank=True)
    is_primary = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-is_primary", "name"]

    def __str__(self):
        return f"{self.name} @ {self.client.name}"
