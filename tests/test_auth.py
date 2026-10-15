from __future__ import annotations

import base64
import threading

import pytest

from pypiswasta import auth as auth_module
from pypiswasta import db
from pypiswasta.auth import (
    AuthStore,
    Credential,
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


# --------------------------------------------------------------------------- #
# authentication work is bounded and kept off the event loop (SR-05)
# --------------------------------------------------------------------------- #


def test_unknown_username_does_not_use_a_slow_kdf_without_argon2_credentials(monkeypatch) -> None:
    """A fast-digest-only store must not offer an unknown-user Argon2 burn."""

    def unexpected_work(*args):
        raise AssertionError("a fast-only snapshot used Argon2")

    monkeypatch.setattr(auth_module, "hash_argon2", unexpected_work)
    monkeypatch.setattr(auth_module, "verify_argon2", unexpected_work)

    store = AuthStore()
    reader, _ = generate_credential("reader")
    admin, _ = generate_credential("admin")
    store.install([_row(reader), _row(admin)])

    assert store.authenticate("ghost", "whatever") is None


def test_unknown_username_is_equalised_when_a_slow_kdf_is_in_use(monkeypatch) -> None:
    """...but an Argon2 deployment still spends comparable time on unknowns."""
    burned: list[str] = []

    def verify(stored: str, password: str) -> bool:
        burned.append(password)
        return False

    monkeypatch.setattr(auth_module, "verify_argon2", verify)

    store = AuthStore()
    reader, _ = generate_credential("reader")
    admin = build_credential("admin", "root", "s3cret-password", algorithm=db.ALGORITHM_ARGON2ID)
    store.install([_row(reader), _row(admin)])

    assert store.authenticate("ghost", "whatever") is None
    assert burned == ["whatever"]


def test_argon2_snapshot_prepares_dummy_hash_before_authentication(monkeypatch) -> None:
    """Installing a cold snapshot prepares its hash before requests can use it."""
    hashes: list[str] = []
    verifications: list[tuple[str, str]] = []

    class _Hasher:
        def hash(self, password: str) -> str:
            hashes.append(password)
            return "prepared-dummy-hash"

        def verify(self, stored: str, password: str) -> bool:
            verifications.append((stored, password))
            return False

    monkeypatch.setattr(auth_module, "_hasher", _Hasher())
    store = AuthStore()
    admin = Credential("admin", "root", db.ALGORITHM_ARGON2ID, "stored-admin-hash")
    store.install([_row(admin)])
    assert len(hashes) == 1
    assert store.authenticate("unknown", "wrong") is None
    assert hashes == ["pypiswasta-dummy-password"]
    assert verifications == [("prepared-dummy-hash", "wrong")]


def test_failed_dummy_hash_preparation_preserves_previous_snapshot(monkeypatch) -> None:
    store = AuthStore()
    reader, password = generate_credential("reader")
    store.install([_row(reader)])

    def fail_hash(value: str) -> str:
        raise RuntimeError("dummy hash initialization failed")

    monkeypatch.setattr(auth_module, "hash_argon2", fail_hash)
    admin = Credential("admin", "root", db.ALGORITHM_ARGON2ID, "stored-admin-hash")
    with pytest.raises(RuntimeError, match="dummy hash initialization failed"):
        store.install([_row(admin)])
    assert store.initialized is False
    assert store.usernames() == {"reader": reader.username}
    assert store.authenticate(reader.username, password) is not None


def test_excess_slow_kdf_work_is_refused_without_waiting(monkeypatch) -> None:
    """When every KDF slot is busy, an extra call returns without running it."""
    capacity = auth_module._ARGON2_MAX_CONCURRENCY
    for _ in range(capacity):
        assert auth_module._argon2_slots.acquire(blocking=False)

    class _NeverCalled:
        def verify(self, stored: str, password: str) -> bool:  # pragma: no cover
            raise AssertionError("the KDF ran without holding a slot")

    monkeypatch.setattr(auth_module, "_hasher", _NeverCalled())
    try:
        # Refused at once (a failed verification), rather than queued in a
        # worker thread waiting for capacity.
        assert auth_module.verify_argon2("stored", "pw") is False
    finally:
        for _ in range(capacity):
            auth_module._argon2_slots.release()


def test_simultaneous_slow_kdf_work_stays_within_the_bound(monkeypatch) -> None:
    """No more than ``_ARGON2_MAX_CONCURRENCY`` KDF calls run at once."""
    bound = auth_module._ARGON2_MAX_CONCURRENCY
    lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()
    state = {"active": 0, "peak": 0}

    class _FakeHasher:
        def verify(self, stored: str, password: str) -> bool:
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                if state["active"] == bound:
                    entered.set()
            release.wait(5)
            with lock:
                state["active"] -= 1
            return False

        def hash(self, password: str) -> str:  # pragma: no cover - not exercised
            return "$argon2id$fake"

    monkeypatch.setattr(auth_module, "_hasher", _FakeHasher())
    monkeypatch.setattr(auth_module, "_argon2_slots", threading.BoundedSemaphore(bound))

    threads = [threading.Thread(target=lambda: auth_module.verify_argon2("stored", "pw")) for _ in range(bound + 3)]
    for thread in threads:
        thread.start()
    assert entered.wait(5), "expected verifications never ran concurrently"
    with lock:
        assert state["active"] == bound  # the surplus callers were refused
    release.set()
    for thread in threads:
        thread.join()

    assert state["peak"] == bound
