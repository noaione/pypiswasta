"""API tokens: CLI management, authentication and the masked summary."""

from __future__ import annotations

import contextlib
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN_USERNAME, Instance, build_wheel, parse_credentials, wheel_filename
from pypiswasta.app import create_app
from pypiswasta.auth import (
    TOKEN_SENTINEL,
    AuthStore,
    Credential,
    fast_password_digest,
    generate_credential,
    generate_token_secret,
    token_hint,
)
from pypiswasta.cli import main
from pypiswasta.db import ALGORITHM_SHA256_DOMAIN

TOKEN_SECRET = re.compile(r"^token:\s+(\S+)$", re.MULTILINE)
TOKEN_SUMMARY = re.compile(r"^summary:\s+(\S+)$", re.MULTILINE)


def run_cli(*args: str) -> tuple[int, str, str]:
    """Invoke the CLI in-process and capture its streams."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


def create_token(config_path: Path, name: str, *extra: str) -> tuple[str, str]:
    """Create a token and return ``(secret, hint)`` from the CLI output."""
    code, out, err = run_cli("token", "create", name, "--config", str(config_path), *extra)
    assert code == 0, err
    secret = TOKEN_SECRET.search(out)
    hint = TOKEN_SUMMARY.search(out)
    assert secret is not None and hint is not None, out
    return secret.group(1), hint.group(1)


def initialize(config_path: Path) -> dict[str, tuple[str, str]]:
    """Run first-time setup so the role usernames exist, then close the app.

    The CLI talks to the same database the service does, so the setup has to
    have happened before commands like ``token create`` have anything to test
    name collisions against.
    """
    with TestClient(create_app(config_path)) as client:
        response = client.post("/setup", data={"username": ADMIN_USERNAME})
        assert response.status_code == 200, response.text
        return parse_credentials(response.text)


@dataclass
class Tokened:
    instance: Instance
    token: str
    hint: str
    output: str


@pytest.fixture
def tokened(config_path: Path) -> Generator[Tokened]:
    """An initialized instance plus one upload token, reloaded as a new process."""
    with TestClient(create_app(config_path)) as client:
        response = client.post("/setup", data={"username": ADMIN_USERNAME})
        assert response.status_code == 200, response.text
        credentials = parse_credentials(response.text)

    code, out, err = run_cli("token", "create", "ci-publish", "--config", str(config_path))
    assert code == 0, err
    secret = TOKEN_SECRET.search(out)
    hint = TOKEN_SUMMARY.search(out)
    assert secret is not None and hint is not None, out

    # A fresh app: the service reads the database into a snapshot at startup.
    with TestClient(create_app(config_path)) as client:
        yield Tokened(
            instance=Instance(client=client, credentials=credentials),
            token=secret.group(1),
            hint=hint.group(1),
            output=out,
        )


# --------------------------------------------------------------------------- #
# token shape
# --------------------------------------------------------------------------- #


def test_generated_token_is_prefixed_and_long() -> None:
    secret = generate_token_secret()
    assert secret.startswith("pypisw_")
    assert len(secret) == len("pypisw_") + 43
    assert generate_token_secret() != secret


def test_hint_shows_only_a_head_and_a_tail() -> None:
    sample = "pypisw_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
    hint = token_hint(sample)

    assert hint == "pypisw_abcd…DEFG"
    assert hint != sample
    # The middle of the token must not be recoverable from the stored hint.
    assert sample[20:30] not in hint


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_create_prints_the_secret_once(config_path: Path) -> None:
    code, out, err = run_cli("token", "create", "ci-publish", "--config", str(config_path))
    assert code == 0, err

    assert re.search(r"^token:\s+pypisw_\S+$", out, re.MULTILINE)
    assert "created token 'ci-publish' with the upload role" in out
    assert "not written to disk or logged in plaintext" in out
    assert "restart the PyPISwasta service" in out
    # The token must be offered under both accepted usernames.
    assert f"username: ci-publish  (or {TOKEN_SENTINEL})" in out


def test_create_honours_the_role_flag(config_path: Path) -> None:
    code, out, err = run_cli("token", "create", "ci-read", "--role", "reader", "--config", str(config_path))
    assert code == 0, err
    assert "with the reader role" in out


def test_list_is_empty_before_any_token(config_path: Path) -> None:
    code, out, _ = run_cli("token", "list", "--config", str(config_path))
    assert code == 0
    assert "no tokens" in out


def test_list_shows_the_summary_not_the_secret(config_path: Path) -> None:
    secret, hint = create_token(config_path, "ci-publish")

    code, out, _ = run_cli("token", "list", "--config", str(config_path))
    assert code == 0
    assert f"ci-publish\tupload\t{hint}\t" in out
    assert secret not in out


def test_remove_deletes_the_token(config_path: Path) -> None:
    create_token(config_path, "ci-publish")

    code, out, err = run_cli("token", "remove", "ci-publish", "--config", str(config_path))
    assert code == 0, err
    assert "removed token ci-publish" in out

    _, listed, _ = run_cli("token", "list", "--config", str(config_path))
    assert "no tokens" in listed


def test_remove_unknown_token_fails(config_path: Path) -> None:
    code, _, err = run_cli("token", "remove", "absent", "--config", str(config_path))
    assert code == 1
    assert "unknown token" in err


@pytest.mark.parametrize("name", ["bad:name", "has space", "", "emoji-\N{SNOWMAN}"])
def test_rejects_unusable_token_names(config_path: Path, name: str) -> None:
    code, _, err = run_cli("token", "create", name, "--config", str(config_path))
    assert code == 1
    assert "Username must be" in err


def test_rejects_the_sentinel_as_a_token_name(config_path: Path) -> None:
    code, _, err = run_cli("token", "create", TOKEN_SENTINEL, "--config", str(config_path))
    assert code == 1
    assert "reserved" in err


def test_rejects_a_name_that_collides_with_a_role_username(config_path: Path) -> None:
    """``reader`` is the reader role's username after setup, so it is taken."""
    initialize(config_path)

    code, _, err = run_cli("token", "create", "reader", "--config", str(config_path))
    assert code == 1
    assert "already used" in err


def test_rejects_a_duplicate_token_name(config_path: Path) -> None:
    create_token(config_path, "ci-publish")

    code, _, err = run_cli("token", "create", "ci-publish", "--config", str(config_path))
    assert code == 1
    assert "already used" in err


def test_role_username_cannot_collide_with_a_token_name(config_path: Path) -> None:
    """The reverse direction: a role username must not steal a token's name."""
    initialize(config_path)
    create_token(config_path, "ci-publish")

    code, _, err = run_cli("auth", "set-username", "reader", "ci-publish", "--config", str(config_path))
    assert code == 1
    assert "already used" in err


# --------------------------------------------------------------------------- #
# authentication
# --------------------------------------------------------------------------- #


def test_token_uploads_under_its_own_name(tokened: Tokened) -> None:
    response = tokened.instance.upload(build_wheel(), auth=("ci-publish", tokened.token))
    assert response.status_code == 200, response.text


def test_token_works_with_the_pypi_sentinel_username(tokened: Tokened) -> None:
    """``__token__`` is what the official GitHub Action sends by default."""
    response = tokened.instance.upload(build_wheel(), auth=(TOKEN_SENTINEL, tokened.token))
    assert response.status_code == 200, response.text


def test_wrong_token_secret_is_rejected(tokened: Tokened) -> None:
    assert tokened.instance.upload(build_wheel(), auth=("ci-publish", "pypisw_wrong")).status_code == 401
    assert tokened.instance.upload(build_wheel(), auth=(TOKEN_SENTINEL, "pypisw_wrong")).status_code == 401


def test_unknown_sentinel_secret_is_rejected(tokened: Tokened) -> None:
    """A ``__token__`` request with no matching token must not fall through."""
    assert tokened.instance.upload(build_wheel(), auth=(TOKEN_SENTINEL, "")).status_code == 401


def test_role_credentials_still_work_alongside_a_token(tokened: Tokened) -> None:
    assert tokened.instance.upload(build_wheel(), role="upload").status_code == 200


def test_reader_token_cannot_upload(config_path: Path) -> None:
    initialize(config_path)
    secret, _ = create_token(config_path, "ci-read", "--role", "reader")

    with TestClient(create_app(config_path)) as client:
        instance = Instance(client=client, credentials={})
        response = instance.upload(build_wheel(), auth=("ci-read", secret))
        assert response.status_code == 403


def test_reader_token_can_read_the_private_index(config_path: Path) -> None:
    credentials = initialize(config_path)
    with TestClient(create_app(config_path)) as client:
        instance = Instance(client=client, credentials=credentials)
        assert instance.upload(build_wheel("secret", "1.0.0"), wheel_filename("secret", "1.0.0")).status_code == 200

    secret, _ = create_token(config_path, "ci-read", "--role", "reader")

    with TestClient(create_app(config_path)) as client:
        assert client.get("/private/simple/", auth=("ci-read", secret)).status_code == 200
        assert client.post("/legacy/", auth=("ci-read", secret)).status_code == 403


def test_admin_token_reaches_the_admin_api(config_path: Path) -> None:
    initialize(config_path)
    secret, _ = create_token(config_path, "ci-admin", "--role", "admin")

    with TestClient(create_app(config_path)) as client:
        response = client.get("/api/admin/packages", auth=("ci-admin", secret))
        assert response.status_code == 200


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #


def test_a_new_token_is_not_live_until_the_service_restarts(config_path: Path) -> None:
    """Documents the in-memory snapshot: the CLI writes, the service must reload."""
    with TestClient(create_app(config_path)) as early:
        assert early.post("/setup", data={"username": ADMIN_USERNAME}).status_code == 200
        code, out, err = run_cli("token", "create", "later", "--config", str(config_path))
        assert code == 0, err
        secret = TOKEN_SECRET.search(out)
        assert secret is not None

        # This app instance loaded its snapshot before the token existed.
        assert early.get("/api/admin/packages", auth=("later", secret.group(1))).status_code == 401


def test_a_removed_token_stops_working_after_a_restart(config_path: Path) -> None:
    initialize(config_path)
    secret, _ = create_token(config_path, "ci-publish")

    with TestClient(create_app(config_path)) as client:
        assert client.get("/api/admin/packages", auth=("ci-publish", secret)).status_code == 403
        assert client.get("/healthz").status_code == 200

    assert run_cli("token", "remove", "ci-publish", "--config", str(config_path))[0] == 0

    with TestClient(create_app(config_path)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/admin/packages", auth=("ci-publish", secret)).status_code == 401


# --------------------------------------------------------------------------- #
# the store itself
# --------------------------------------------------------------------------- #


def role_row(credential: Credential) -> dict[str, str]:
    return {
        "role": credential.role,
        "username": credential.username,
        "algorithm": credential.algorithm,
        "password_hash": credential.password_hash,
    }


def token_row(name: str, role: str, secret: str) -> dict[str, str]:
    return {
        "name": name,
        "role": role,
        "algorithm": ALGORITHM_SHA256_DOMAIN,
        "password_hash": fast_password_digest(role, secret),
    }


def test_store_matches_tokens_by_secret_and_keeps_the_role_credential() -> None:
    store = AuthStore()
    admin, admin_password = generate_credential("admin")
    reader, reader_password = generate_credential("reader")
    secret = generate_token_secret()

    store.install(
        [role_row(admin), role_row(reader)],
        [token_row("ci-publish", "upload", secret)],
    )

    assert store.initialized is True
    assert store.authenticate("ci-publish", secret) is not None
    assert store.authenticate(TOKEN_SENTINEL, secret) is not None
    assert store.authenticate("ci-publish", "nope") is None
    assert store.authenticate(TOKEN_SENTINEL, "nope") is None

    # Role credentials keep working, and the token did not displace any of them.
    assert store.authenticate(admin.username, admin_password) is not None
    assert store.authenticate(reader.username, reader_password) is not None
    assert store.usernames() == {"admin": "admin", "reader": "reader"}
    assert store.credential_for_role("reader") is not None
    assert store.credential_for_role("reader").username == "reader"  # type: ignore[union-attr]


def test_store_lets_a_token_carry_the_reader_role() -> None:
    store = AuthStore()
    admin, _ = generate_credential("admin")
    secret = generate_token_secret()
    store.install([role_row(admin)], [token_row("ci-read", "reader", secret)])

    credential = store.authenticate(TOKEN_SENTINEL, secret)
    assert credential is not None
    assert credential.role == "reader"
    assert credential.username == "ci-read"


def test_store_refuses_a_token_that_shadows_a_username() -> None:
    store = AuthStore()
    upload, _ = generate_credential("upload")

    with pytest.raises(ValueError, match="collides"):
        store.install(
            [role_row(upload)],
            [token_row(upload.username, "upload", generate_token_secret())],
        )


def test_store_authenticates_tokens_after_a_reload() -> None:
    """The snapshot swap must carry tokens over, not just role credentials."""
    store = AuthStore()
    upload, _ = generate_credential("upload")
    secret = generate_token_secret()

    store.install([role_row(upload)], [token_row("ci-publish", "upload", secret)])
    assert store.authenticate("ci-publish", secret) is not None

    store.install([role_row(upload)])
    assert store.authenticate("ci-publish", secret) is None
