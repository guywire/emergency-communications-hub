"""Shared pytest setup."""

import pytest


@pytest.fixture(autouse=True)
def _known_first_admin_password(monkeypatch):
    # Production generates a random first-admin password (ech/core/auth.py);
    # tests log in as admin/admin, so pin it here.
    monkeypatch.setenv("ECH_INITIAL_ADMIN_PASSWORD", "admin")
