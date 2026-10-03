"""Saying nothing happened, when nothing happened.

A PATCH naming a field that does not exist — a typo, a renamed field, a
client written against an older shape — is dropped in silence and answers
200 with the unchanged record. The screen shows "Saved", the person
believes the change is in, and it is not. An error would have told them.

Deliberately narrower than "changed nothing". A field the serializer knows
but will not write for this person is ignored on purpose: a form posts back
everything it holds, including what its user may not edit, and refusing
those would break every such form. That case is a rule being applied, and
the tests across this codebase assert it. A field the serializer has never
heard of is nobody's rule — it is a caller that is wrong.
"""
from rest_framework.exceptions import ValidationError


class RefusesSilentNoOps:
    """Mix in before ``ModelViewSet``. PATCH only.

    PUT is a whole-record write; a PATCH is a statement that something in
    particular should change.
    """

    def partial_update(self, request, *args, **kwargs):
        sent = set(request.data or {})
        if sent:
            serializer = self.get_serializer()
            known = set(serializer.fields)
            known |= {f.source for f in serializer.fields.values() if f.source}
            unknown = sent - known
            if unknown and not (sent & known):
                names = ", ".join(sorted(unknown))
                raise ValidationError({
                    "detail": (
                        f"No such field: {names}. Nothing was changed."
                    )
                })
        return super().partial_update(request, *args, **kwargs)
