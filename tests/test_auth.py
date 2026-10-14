from __future__ import annotations

import base64

from pypiswasta import db
from pypiswasta.auth import (
    AuthStore,
    build_credential,
    fast_password_digest,
    generate_credential,
    generate_password,
    parse_basic_auth,
    verify_credential,
)


def _row(credential) -> dict:
    return {
        "role": credential.role,
        "username": credential.username,
        "algorithm": credential.algorithm,
        "password_hash": credential.password_hash,
    }


def test_generated_password_is_strong() -> None:
    password = generate_password()
    assert len(password) >= 42
    assert generate_password() != password


def test_domain_separated_digest_differs_by_role() -> None:
    assert fast_password_digest("reader", "same") != fast_password_digest("upload", "same")
    assert fast_password_digest("reader", "same") != fast_password_digest("reader", "other")


def test_generated_credential_roundtrip() -> None:
    credential, password = generate_credential("reader")
    assert credential.role == "reader"
    assert credential.username == "reader"
    assert credential.algorithm == db.ALGORITHM_SHA256_DOMAIN
    assert verify_credential(credential, password)
    assert not verify_credential(credential, password + "x")


def test_argon2_credential_roundtrip() -> None:
    credential = build_credential("admin", "admin", "correct horse battery", algorithm=db.ALGORITHM_ARGON2ID)
    assert credential.password_hash.startswith("$argon2id$")
    assert verify_credential(credential, "correct horse battery")
    assert not verify_credential(credential, "incorrect horse battery")


def test_custom_username_is_respected() -> None:
    credential, _ = generate_credential("upload", username="publisher-2")
    assert credential.username == "publisher-2"
    assert credential.role == "upload"


def test_auth_store_authenticates_and_reports_initialization() -> None:
    store = AuthStore()
    reader, reader_password = generate_credential("reader")
    admin = build_credential("admin", "root", "s3cret-password", algorithm=db.ALGORITHM_ARGON2ID)

    assert store.initialized is False

    store.install([_row(reader), _row(admin)])

    assert store.initialized is True
    assert store.authenticate("reader", reader_password) is not None
    assert store.authenticate("reader", "wrong-password") is None
    assert store.authenticate("root", "s3cret-password") is not None
    assert store.authenticate("nobody", "whatever") is None
    admin_credential = store.credential_for_role("admin")
    assert admin_credential is not None
    assert admin_credential.username == "root"
    assert store.usernames() == {"reader": "reader", "admin": "root"}


def test_auth_store_without_admin_is_uninitialized() -> None:
    store = AuthStore()
    reader, _ = generate_credential("reader")
    store.install([_row(reader)])
    assert store.initialized is False


def test_auth_store_snapshot_swap_is_atomic() -> None:
    store = AuthStore()
    reader, reader_password = generate_credential("reader")
    admin = build_credential("admin", "root", "s3cret-password", algorithm=db.ALGORITHM_ARGON2ID)
    store.install([_row(reader), _row(admin)])

    rotated, rotated_password = generate_credential("reader", username="reader")

    # An invalid snapshot must not be installed (duplicate usernames).
    try:
        store.install([_row(rotated), _row(rotated)])
    except ValueError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("duplicate usernames must be rejected")

    # The previous snapshot is still intact.
    assert store.authenticate("reader", reader_password) is not None

    store.install([_row(rotated), _row(admin)])
    assert store.authenticate("reader", rotated_password) is not None
    assert store.authenticate("reader", reader_password) is None


def test_parse_basic_auth() -> None:
    encoded = base64.b64encode(b"user:pa:ss").decode()
    assert parse_basic_auth(f"Basic {encoded}") == ("user", "pa:ss")
    assert parse_basic_auth(f"basic {encoded}") == ("user", "pa:ss")
    assert parse_basic_auth(None) is None
    assert parse_basic_auth("") is None
    assert parse_basic_auth("Bearer token") is None
    assert parse_basic_auth("Basic") is None
    assert parse_basic_auth("Basic !!!not-base64!!!") is None
    assert parse_basic_auth("Basic " + base64.b64encode(b"noseparator").decode()) is None
    assert parse_basic_auth("Basic " + base64.b64encode(b"user:\xff").decode()) is None
