"""Authentication helpers exported by the auth package.

This module exposes commonly used authentication helpers for the
application and keeps the package's public API tidy.
"""

from .user_management import (
    ensure_user_in_organizations,
    update_identity_last_login,
    validate_user,
    token_required,
)
from .oauth_handlers import setup_oauth_handlers

__all__ = [
    "ensure_user_in_organizations",
    "update_identity_last_login",
    "validate_user",
    "token_required",
    "setup_oauth_handlers",
]
