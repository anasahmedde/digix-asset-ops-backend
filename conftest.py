import pytest


@pytest.fixture(autouse=True)
def _field_work_from_anywhere(settings):
    """The suites exercise the rules of the work, not the attendance register."""
    settings.FIELD_WORK_NEEDS_CHECK_IN = False


@pytest.fixture(autouse=True)
def _no_suite(settings):
    """A developer's .env may point at a running Suite; tests never call it."""
    settings.SUITE_URL = ""
