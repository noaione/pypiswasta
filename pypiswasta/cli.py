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
import sys
import time
from pathlib import Path

from packaging.utils import canonicalize_name

from . import __version__
from .auth import (
    DEFAULT_USERNAMES,
    MIN_PASSWORD_LENGTH,
    build_credential,
    generate_credential,
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
from .db import ALGORITHM_ARGON2ID, ROLES, Database
from .storage import METADATA as METADATA_KIND
from .storage import OBJECT as OBJECT_KIND
from .storage import ObjectStore

DEFAULT_DATA_DIR = "/var/lib/pypiswasta"


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
    with database.write() as conn:
        database.upsert_credential(
            conn,
            role=role,
            username=args.username,
            algorithm=existing["algorithm"],
            password_hash=existing["password_hash"],
        )
    print(f"{role} username set to {args.username}")
    print("restart the PyPISwasta service to make this take effect")
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

    total = 0
    for kind, digest, path, size in candidates:
        total += size
        if args.dry_run:
            print(f"would remove {kind} {digest} ({size} bytes)")
        else:
            path.unlink()
            print(f"removed {kind} {digest} ({size} bytes)")
    verb = "would free" if args.dry_run else "freed"
    print(f"{verb} {total} bytes from {len(candidates)} unreferenced blob(s)")
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
