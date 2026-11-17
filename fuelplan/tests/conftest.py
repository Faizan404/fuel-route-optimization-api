import pytest
from django.core.cache import cache
from django.core.management import call_command


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Load the real station and place data into the test database once."""
    with django_db_blocker.unblock():
        call_command("load_data", verbosity=0)


@pytest.fixture(autouse=True)
def _database_and_clean_cache(db):
    cache.clear()
    yield
    cache.clear()
