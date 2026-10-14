"""Credential hashing, the in-memory auth snapshot and HTTP Basic dependencies.

Credentials are stored in SQLite but *only* read at startup (and on reload) into
an immutable in-memory snapshot. No request ever performs a database lookup for
authentication.

* Every credential created by first-time setup or ``auth reset-password`` is a
  256-bit random secret stored as a domain-separated SHA-256 digest. Such a
  secret cannot be brute forced offline, so a slow KDF buys nothing and would
  only slow down legitimate auth.
* Only a *human-chosen* administrator password (``auth set-admin-password``) is
  stored as an Argon2id hash. The algorithm is recorded explicitly so a
  user-supplied password is never silently downgraded.
"""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)
from fastapi import HTTPException, Request

from . import db as db_module

_DOMAIN_SEPARATOR = b"pypiswasta:password:v1:"

# Roles are deliberately *not* a linear hierarchy: ``upload`` may publish but
# must never read private projects, so every guard lists the roles it accepts.
ALLOWED_ROLES: dict[str, frozenset[str]] = {
    "reader": frozenset({"reader", "admin"}),
    "upload": frozenset({"upload", "admin"}),
    "admin": frozenset({"admin"}),
}

DEFAULT_USERNAMES = {"admin": "admin", "upload": "publisher", "reader": "reader"}

MIN_PASSWORD_LENGTH = 8

# HTTP Basic splits the credential on the first colon, so a colon can never be
# part of a username. Keep the alphabet tight and reject anything else outright.
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._@+-]{1,64}$")
USERNAME_RULE = "1-64 characters using letters, digits, or . _ @ + -"


class UsernameError(ValueError):
    """Raised when a credential username cannot be used in HTTP Basic auth."""


def validate_username(value: str) -> str:
    """Trim and validate a username, raising :class:`UsernameError` if unusable."""
    username = (value or "").strip()
    if not USERNAME_PATTERN.match(username):
        raise UsernameError(f"Username must be {USERNAME_RULE}")
    return username


_hasher = PasswordHasher()


# --------------------------------------------------------------------------- #
# password hashing
# --------------------------------------------------------------------------- #


def generate_password() -> str:
    """A 256-bit URL-safe secret (43 characters)."""
    return secrets.token_urlsafe(32)


def fast_password_digest(role: str, password: str) -> str:
    """Domain-separated SHA-256 digest for machine generated secrets."""
    payload = _DOMAIN_SEPARATOR + role.encode("utf-8") + b":" + password.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify_fast_digest(role: str, stored: str, password: str) -> bool:
    return hmac.compare_digest(stored, fast_password_digest(role, password))


def hash_argon2(password: str) -> str:
    return _hasher.hash(password)


def verify_argon2(stored: str, password: str) -> bool:
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception:  # pragma: no cover - defensive
        return False


@functools.lru_cache(maxsize=1)
def _dummy_argon2_hash() -> str:
    """A real Argon2id hash used to equalise work for unknown usernames."""
    return _hasher.hash("pypiswasta-dummy-password")


def _burn_unknown_user_time(password: str) -> None:
    verify_argon2(_dummy_argon2_hash(), password)


@dataclass(frozen=True, slots=True)
class Credential:
    role: str
    username: str
    algorithm: str
    password_hash: str


def verify_credential(credential: Credential, password: str) -> bool:
    if credential.algorithm == db_module.ALGORITHM_ARGON2ID:
        return verify_argon2(credential.password_hash, password)
    if credential.algorithm == db_module.ALGORITHM_SHA256_DOMAIN:
        return verify_fast_digest(credential.role, credential.password_hash, password)
    return False


def build_credential(role: str, username: str, password: str, *, algorithm: str) -> Credential:
    if algorithm == db_module.ALGORITHM_ARGON2ID:
        digest = hash_argon2(password)
    elif algorithm == db_module.ALGORITHM_SHA256_DOMAIN:
        digest = fast_password_digest(role, password)
    else:  # pragma: no cover - guarded by callers
        raise ValueError(f"unsupported algorithm: {algorithm}")
    return Credential(role=role, username=username, algorithm=algorithm, password_hash=digest)


def generate_credential(role: str, *, username: str | None = None) -> tuple[Credential, str]:
    """Create a role credential with a freshly generated password.

    Returns ``(credential, plaintext_password)``. The plaintext is returned to
    the caller exactly once and never persisted.
    """
    if role not in db_module.ROLES:
        raise ValueError(f"unknown role: {role}")
    password = generate_password()
    credential = build_credential(
        role,
        username or DEFAULT_USERNAMES[role],
        password,
        algorithm=db_module.ALGORITHM_SHA256_DOMAIN,
    )
    return credential, password


# --------------------------------------------------------------------------- #
# in-memory snapshot
# --------------------------------------------------------------------------- #


class _RowLike(Protocol):
    """Anything indexable by column name: a ``sqlite3.Row`` or a plain mapping."""

    def __getitem__(self, key: str) -> Any: ...


class AuthStore:
    """Immutable username -> credential map with atomic snapshot swaps."""

    def __init__(self) -> None:
        self._by_username: dict[str, Credential] = {}
        self._by_role: dict[str, Credential] = {}
        self._initialized = False

    def install(self, rows: Iterable[_RowLike]) -> None:
        by_username: dict[str, Credential] = {}
        by_role: dict[str, Credential] = {}
        for row in rows:
            credential = Credential(
                role=row["role"],
                username=row["username"],
                algorithm=row["algorithm"],
                password_hash=row["password_hash"],
            )
            if credential.username in by_username:
                raise ValueError(f"duplicate username in credential store: {credential.username}")
            by_username[credential.username] = credential
            by_role[credential.role] = credential

        # Atomic swap: a single attribute rebind is observed by other threads.
        self._by_username = by_username
        self._by_role = by_role
        self._initialized = "admin" in by_role

    def reload(self, database: db_module.Database) -> None:
        self.install(database.list_credentials())  # pyright: ignore[reportArgumentType]

    @property
    def initialized(self) -> bool:
        """True once an administrator credential exists."""
        return self._initialized

    def credential_for_role(self, role: str) -> Credential | None:
        return self._by_role.get(role)

    def usernames(self) -> dict[str, str]:
        return {role: cred.username for role, cred in self._by_role.items()}

    def authenticate(self, username: str, password: str) -> Credential | None:
        credential = self._by_username.get(username)
        if credential is None:
            _burn_unknown_user_time(password)
            return None
        if verify_credential(credential, password):
            return credential
        return None


# --------------------------------------------------------------------------- #
# HTTP Basic
# --------------------------------------------------------------------------- #


def parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    if not header:
        return None
    scheme, _, params = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    token = params.strip()
    if not token:
        return None
    try:
        decoded = base64.b64decode(token, validate=True)
    except (binascii.Error, ValueError):
        return None
    username, separator, password = decoded.partition(b":")
    if not separator:
        return None
    try:
        return username.decode("utf-8"), password.decode("utf-8")
    except UnicodeDecodeError:
        return None


def unauthorized(request: Request) -> HTTPException:
    """401 with a challenge that names the configured branding."""
    realm = request.app.state.config.branding.realm
    return HTTPException(
        status_code=401,
        detail="Authentication required",
        headers={"WWW-Authenticate": f'Basic realm="{realm}"'},
    )


def forbidden() -> HTTPException:
    return HTTPException(status_code=403, detail="Insufficient permissions")


def _current_credential(request: Request) -> Credential:
    parsed = parse_basic_auth(request.headers.get("authorization"))
    if parsed is None:
        raise unauthorized(request)
    store: AuthStore = request.app.state.auth
    credential = store.authenticate(*parsed)
    if credential is None:
        raise unauthorized(request)
    return credential


def _require(capability: str):
    allowed = ALLOWED_ROLES[capability]

    def dependency(request: Request) -> Credential:
        credential = _current_credential(request)
        if credential.role not in allowed:
            raise forbidden()
        return credential

    return dependency


require_reader = _require("reader")
require_upload = _require("upload")
require_admin = _require("admin")
