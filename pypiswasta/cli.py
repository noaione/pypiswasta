"""``pypiswasta`` command line interface.

The CLI and the HTTP service share the same database and storage modules, so
project and credential management behave identically either way. Because
credentials live in SQLite, ``auth`` commands can safely run while the service
is up (WAL plus short write transactions).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
import time
from pathlib import Path

from packaging.utils import canonicalize_name

from . import __version__
from .auth import (
    DEFAULT_USERNAMES,
    MIN_PASSWORD_LENGTH,
    TOKEN_SENTINEL,
    UsernameError,
    build_credential,
    generate_credential,
    generate_token_secret,
    token_hint,
    validate_username,
)
from .config import (
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    default_config,
    load_config,
    render_config,
    write_config_atomic,
)
from .db import ALGORITHM_ARGON2ID, ALGORITHM_SHA256_DOMAIN, ROLES, Database
from .storage import METADATA as METADATA_KIND
from .storage import OBJECT as OBJECT_KIND
from .storage import ObjectStore

DEFAULT_DATA_DIR = "/var/lib/pypiswasta"

# Credentials are loaded into an in-memory snapshot at startup, so any command
# that changes them needs the service restarted (there is no SIGHUP reload).
_RESTART_HINT = (
    "restart the PyPISwasta service to make this take effect; credentials are "
    "cached in memory and the database is only read at startup"
)


def _open_db(config: Config) -> Database:
    database = Database(config.storage.database, config.performance.sqlite_busy_timeout_ms)
    database.migrate()
    return database


def _open_store(config: Config) -> ObjectStore:
    store = ObjectStore(config.storage.data_dir)
    store.ensure_layout()
    return store


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def _cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config)
    if path.exists() and not args.force:
        print(f"error: configuration already exists: {path}", file=sys.stderr)
        print("re-run with --force to overwrite it", file=sys.stderr)
        return 1

    data_dir = Path(args.data_dir)
    database_path = Path(args.database) if args.database else data_dir / "packages.db"
    config = default_config(data_dir, database=database_path)

    write_config_atomic(path, render_config(config))
    _open_store(config)
    _open_db(config)

    print(f"wrote configuration: {path}")
    print(f"data directory:      {config.storage.data_dir}")
    print(f"database:            {config.storage.database}")
    print()
    print("Next steps:")
    print(f"  1. start the service: pypiswasta serve --config {path}")
    print("  2. open the setup page and choose the administrator username:")
    print(f"     http://{config.server.host}:{config.server.port}/setup")
    print("     All three passwords are generated and shown exactly once.")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    # The Granian worker imports the app factory, which reads this variable.
    os.environ["PYPISWASTA_CONFIG"] = str(Path(args.config).resolve())

    from granian import Granian
    from granian.constants import Interfaces

    server = Granian(
        "pypiswasta.app:create_app",
        address=config.server.host,
        port=config.server.port,
        interface=Interfaces.ASGI,
        workers=config.server.workers,
        factory=True,
        log_access=False,
    )
    server.serve()
    return 0


def _cmd_auth_list(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    database = _open_db(config)
    rows = database.list_credentials()
    if not rows:
        print(
            "no credentials configured; start the service and complete first-time setup",
            file=sys.stderr,
        )
        return 1
    for row in rows:
        print(f"{row['role']}\t{row['username']}")
    return 0


def _cmd_auth_reset_password(args: argparse.Namespace) -> int:
    """Generate a new random password for a role and print it exactly once.

    Works for every role including ``admin`` and never prompts, so it is safe to
    script. ``set-admin-password`` remains the way to choose your own password.
    """
    config = load_config(args.config)
    database = _open_db(config)
    role = args.role
    existing = database.get_credential(role)
    username = existing["username"] if existing else DEFAULT_USERNAMES[role]
    credential, password = generate_credential(role, username=username)
    with database.write() as conn:
        database.upsert_credential(
            conn,
            role=credential.role,
            username=credential.username,
            algorithm=credential.algorithm,
            password_hash=credential.password_hash,
        )
    print(f"generated a new {role} password; the previous one no longer works")
    print(f"username: {credential.username}")
    print(f"password: {password}")
    print("store it now; it is not written to disk or logged in plaintext")
    print(
        "restart the PyPISwasta service to make this take effect; credentials are "
        "cached in memory and the database is only read at startup"
    )
    return 0


def _cmd_auth_set_username(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    database = _open_db(config)
    role = args.role
    existing = database.get_credential(role)
    if existing is None:
        print(f"error: no {role} credential is configured", file=sys.stderr)
        return 1

    try:
        username = validate_username(args.username)
    except UsernameError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if existing["username"] == username:
        print(f"{role} username is already {username}")
        return 0

    # Token names live in the same username namespace, so a collision here would
    # make the in-memory snapshot ambiguous. The credentials table only guards
    # against other role usernames, hence the explicit check.
    if username in database.usernames_in_use():
        print(
            f"error: username {username!r} is already used by another credential or token",
            file=sys.stderr,
        )
        return 1

    try:
        with database.write() as conn:
            database.upsert_credential(
                conn,
                role=role,
                username=username,
                algorithm=existing["algorithm"],
                password_hash=existing["password_hash"],
            )
    except sqlite3.IntegrityError:
        print(
            f"error: username {username!r} is already used by another role",
            file=sys.stderr,
        )
        return 1

    print(f"{role} username set to {username}")
    print("update any client that saved the old username, then restart the service")
    return 0


def _cmd_auth_set_admin_password(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    database = _open_db(config)
    existing = database.get_credential("admin")
    username = existing["username"] if existing else DEFAULT_USERNAMES["admin"]

    while True:
        password = getpass.getpass("New admin password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters",
                file=sys.stderr,
            )
            continue
        confirm = getpass.getpass("Confirm admin password: ")
        if password != confirm:
            print("passwords do not match", file=sys.stderr)
            continue
        break

    credential = build_credential("admin", username, password, algorithm=ALGORITHM_ARGON2ID)
    with database.write() as conn:
        database.upsert_credential(
            conn,
            role=credential.role,
            username=credential.username,
            algorithm=credential.algorithm,
            password_hash=credential.password_hash,
        )
    print("admin password updated")
    return 0


def _cmd_token_create(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    database = _open_db(config)

    try:
        name = validate_username(args.name)
    except UsernameError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if name == TOKEN_SENTINEL:
        print(
            f"error: {TOKEN_SENTINEL!r} is reserved for presenting a token as the password",
            file=sys.stderr,
        )
        return 1
    if name in database.usernames_in_use():
        print(
            f"error: {name!r} is already used by another credential or token",
            file=sys.stderr,
        )
        return 1

    secret = generate_token_secret()
    credential = build_credential(args.role, name, secret, algorithm=ALGORITHM_SHA256_DOMAIN)
    with database.write() as conn:
        database.insert_token(
            conn,
            name=credential.username,
            role=credential.role,
            algorithm=credential.algorithm,
            password_hash=credential.password_hash,
            hint=token_hint(secret),
        )

    print(f"created token {name!r} with the {credential.role} role")
    print(f"token:   {secret}")
    print(f"summary: {token_hint(secret)}")
    print()
    print(f"username: {name}  (or {TOKEN_SENTINEL})")
    print("password: the token above")
    print("store it now; it is not written to disk or logged in plaintext")
    print(_RESTART_HINT)
    return 0


def _cmd_token_list(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    database = _open_db(config)
    rows = database.list_tokens()
    if not rows:
        print("no tokens")
        return 0
    for row in rows:
        print(f"{row['name']}\t{row['role']}\t{row['hint']}\t{row['created_at']}")
    return 0


def _cmd_token_remove(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    database = _open_db(config)
    with database.write() as conn:
        removed = database.delete_token(conn, args.name)
    if not removed:
        print(f"error: unknown token: {args.name}", file=sys.stderr)
        return 1
    print(f"removed token {args.name}")
    print(_RESTART_HINT)
    return 0


def _cmd_package_list(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    database = _open_db(config)
    rows = database.list_projects()
    if not rows:
        print("no projects")
        return 0
    for row in rows:
        visibility = "private" if row["private"] else "public"
        count = database.count_artifacts(row["id"])
        print(f"{visibility:<7} {row['name']}\t{count} file(s)")
    return 0


def _cmd_package_visibility(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    database = _open_db(config)
    name = canonicalize_name(args.name)
    private = args.visibility == "private"
    with database.write() as conn:
        updated = database.set_package_private(conn, name, private)
    if not updated:
        print(f"error: unknown project: {name}", file=sys.stderr)
        return 1
    print(f"{name} is now {args.visibility}")
    return 0


def _cmd_package_delete(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    database = _open_db(config)
    name = canonicalize_name(args.name)
    package = database.get_package(name)
    if package is None:
        print(f"error: unknown project: {name}", file=sys.stderr)
        return 1
    count = database.count_artifacts(package["id"])
    if count and not args.force:
        print(
            f"error: {name} still has {count} published file(s)",
            file=sys.stderr,
        )
        print("re-run with --force to remove the records anyway", file=sys.stderr)
        print(
            "(objects stay on disk until they become unreferenced and are garbage collected)",
            file=sys.stderr,
        )
        return 1
    with database.write() as conn:
        database.delete_package(conn, name)
    print(f"deleted project {name}")
    return 0


def _cmd_gc(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    database = _open_db(config)
    store = _open_store(config)

    # First pass (unlocked): identify plausible garbage. This is only a
    # snapshot -- a publisher may make one of these blobs live again before we
    # act, so the reference check is repeated under the write lock below.
    referenced_wheels, referenced_metadata = database.referenced_digests()
    now = time.time()
    candidates: list[tuple[str, str, Path, int]] = []
    for kind, referenced in (
        (OBJECT_KIND, referenced_wheels),
        (METADATA_KIND, referenced_metadata),
    ):
        for digest, path in store.iter_blobs(kind):
            if digest in referenced:
                continue
            stat = path.stat()
            if now - stat.st_mtime < args.grace_seconds:
                continue
            candidates.append((kind, digest, path, stat.st_size))

    if not candidates:
        print("nothing to collect")
        return 0

    if args.dry_run:
        total = sum(size for _kind, _digest, _path, size in candidates)
        for kind, digest, _path, size in candidates:
            print(f"would remove {kind} {digest} ({size} bytes)")
        print(f"would free {total} bytes from {len(candidates)} unreferenced blob(s)")
        return 0

    # Deletion must be coordinated with publishing. An upload publishes its
    # objects and inserts the reference inside a single BEGIN IMMEDIATE
    # transaction, so taking the same write lock here lets us re-read the live
    # references and unlink while no publisher can add a reference to a
    # candidate. This is what protects a blob that a re-upload made live again:
    # reusing an existing content-addressed object keeps its old modification
    # time, so the grace period alone would not spare it.
    removed: list[tuple[str, str, int]] = []
    total = 0
    with database.write():
        live_wheels, live_metadata = database.referenced_digests()
        live = {OBJECT_KIND: live_wheels, METADATA_KIND: live_metadata}
        for kind, digest, path, size in candidates:
            if digest in live[kind]:
                continue
            try:
                path.unlink()
            except FileNotFoundError:  # pragma: no cover - concurrent removal
                continue
            removed.append((kind, digest, size))
            total += size

    for kind, digest, size in removed:
        print(f"removed {kind} {digest} ({size} bytes)")
    print(f"freed {total} bytes from {len(removed)} unreferenced blob(s)")
    return 0


# --------------------------------------------------------------------------- #
# argparse wiring
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        metavar="PATH",
        help="path to config.toml (default: %(default)s)",
    )

    parser = argparse.ArgumentParser(
        prog="pypiswasta",
        description="Minimal wheel-only Python package index.",
    )
    parser.add_argument("--version", action="version", version=f"pypiswasta {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    init = subparsers.add_parser("init", parents=[common], help="create a configuration file and database")
    init.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        metavar="PATH",
        help="storage directory for objects and the database (default: %(default)s)",
    )
    init.add_argument("--database", default=None, metavar="PATH", help="database file")
    init.add_argument("--force", action="store_true", help="overwrite an existing configuration file")
    init.set_defaults(handler=_cmd_init)

    serve = subparsers.add_parser("serve", parents=[common], help="run the HTTP service")
    serve.set_defaults(handler=_cmd_serve)

    auth = subparsers.add_parser("auth", help="manage credentials")
    auth_sub = auth.add_subparsers(dest="auth_command")

    auth_list = auth_sub.add_parser("list", parents=[common], help="list role usernames")
    auth_list.set_defaults(handler=_cmd_auth_list)

    auth_rotate = auth_sub.add_parser("rotate", parents=[common], help="alias of reset-password")
    auth_rotate.add_argument("role", choices=ROLES)
    auth_rotate.set_defaults(handler=_cmd_auth_reset_password)

    auth_reset = auth_sub.add_parser(
        "reset-password",
        parents=[common],
        help="generate and print a new password for a role (admin included)",
    )
    auth_reset.add_argument("role", choices=ROLES)
    auth_reset.set_defaults(handler=_cmd_auth_reset_password)

    auth_username = auth_sub.add_parser("set-username", parents=[common], help="change the username for a role")
    auth_username.add_argument("role", choices=ROLES)
    auth_username.add_argument("username")
    auth_username.set_defaults(handler=_cmd_auth_set_username)

    auth_admin = auth_sub.add_parser(
        "set-admin-password",
        parents=[common],
        help="set the administrator password interactively",
    )
    auth_admin.set_defaults(handler=_cmd_auth_set_admin_password)

    token = subparsers.add_parser("token", help="manage API tokens")
    token_sub = token.add_subparsers(dest="token_command")

    token_create = token_sub.add_parser(
        "create",
        parents=[common],
        help="create a token and print it exactly once",
    )
    token_create.add_argument("name", help="token name; also its HTTP Basic username")
    token_create.add_argument(
        "--role",
        choices=ROLES,
        default="upload",
        help="capability the token carries (default: %(default)s)",
    )
    token_create.set_defaults(handler=_cmd_token_create)

    token_list = token_sub.add_parser(
        "list",
        parents=[common],
        help="list tokens with a recognisable summary of each secret",
    )
    token_list.set_defaults(handler=_cmd_token_list)

    token_remove = token_sub.add_parser("remove", parents=[common], help="delete a token")
    token_remove.add_argument("name")
    token_remove.set_defaults(handler=_cmd_token_remove)

    package = subparsers.add_parser("package", help="inspect and manage projects")
    package_sub = package.add_subparsers(dest="package_command")

    package_list = package_sub.add_parser("list", parents=[common], help="list projects")
    package_list.set_defaults(handler=_cmd_package_list)

    package_visibility = package_sub.add_parser("visibility", parents=[common], help="change project visibility")
    package_visibility.add_argument("name")
    package_visibility.add_argument("visibility", choices=["public", "private"])
    package_visibility.set_defaults(handler=_cmd_package_visibility)

    package_delete = package_sub.add_parser("delete", parents=[common], help="delete a project record")
    package_delete.add_argument("name")
    package_delete.add_argument("--force", action="store_true", help="delete even if files are published")
    package_delete.set_defaults(handler=_cmd_package_delete)

    gc = subparsers.add_parser("gc", parents=[common], help="remove unreferenced objects from disk")
    gc.add_argument("--dry-run", action="store_true", help="only report what would go")
    gc.add_argument(
        "--grace-seconds",
        type=float,
        default=3600.0,
        metavar="SECONDS",
        help="skip blobs younger than this (default: %(default)s)",
    )
    gc.set_defaults(handler=_cmd_gc)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("interrupted", file=sys.stderr)
        return 130
