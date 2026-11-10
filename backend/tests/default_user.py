"""The account most tests act as (see the `default_user` fixture in conftest.py).

It is created first after the tables are emptied (identities restart), so its ID is always 1.
Tests that save or search documents directly pass this ID as the owner.
"""

DEFAULT_USER_ID = 1
DEFAULT_USER_EMAIL = "tester@example.com"
