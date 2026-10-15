# PyPISwasta

A small Python package index for hosting your own wheels and source distributions.
Publish with `uv` or `twine`, install with `uv` or `pip`, and choose which projects
are public. Private packages require a reader or administrator credential.

PyPISwasta runs as a Python service with SQLite and local file storage. It needs
**Python 3.13 or newer**; the instructions below use [uv](https://docs.astral.sh/uv/).

## Quick start

Run these commands from a checkout of this repository:

```sh
uv sync --locked --no-dev
source .venv/bin/activate

pypiswasta init --config ./config.toml --data-dir "$PWD/data"
pypiswasta serve --config ./config.toml
```

Open <http://127.0.0.1:8374/setup>, choose an administrator username, and save the
three generated passwords in your password manager. **They are shown only once.**
Use the usernames displayed on that page; the defaults for upload and reader
access are `publisher` and `reader` unless those names are already taken.

| Credential | Read private packages | Publish | Administer |
| --- | --- | --- | --- |
| `admin` | Yes | Yes | Yes |
| `upload` | No | Yes | No |
| `reader` | Yes | No | No |

Until setup is complete, package endpoints are unavailable; `/healthz` remains
reachable. On a remote server, complete setup through an SSH tunnel before
exposing the service publicly:

```sh
ssh -L 8374:127.0.0.1:8374 user@your-server
```

Then open the same localhost setup URL on your own computer.

## Deploy on a server

Keep the application on `127.0.0.1:8374` and expose it through an HTTPS reverse
proxy. Use one worker and a dedicated service account with write access to the
data directory. The service account also needs access to the installed Python
interpreter, checkout and virtual environment.

### Run with systemd (Linux)

This example assumes a checkout at `/opt/pypiswasta` with its virtual environment
created there using `uv sync --locked --no-dev`. Use a Python installation
accessible to the service account, rather than one inside another user's private
home directory.

Create the account and initialize persistent storage:

```sh
sudo useradd --system --home-dir /var/lib/pypiswasta --shell /usr/sbin/nologin pypiswasta
sudo install -d -o pypiswasta -g pypiswasta -m 0750 /etc/pypiswasta /var/lib/pypiswasta
sudo -u pypiswasta /opt/pypiswasta/.venv/bin/pypiswasta init
```

The default configuration is `/etc/pypiswasta/config.toml`; the database and stored
files live under `/var/lib/pypiswasta`. For an existing installation, reuse its
configuration and data instead of running `init` again.

Save this as `/etc/systemd/system/pypiswasta.service`:

```ini
[Unit]
Description=PyPISwasta package index
After=network.target

[Service]
Type=simple
User=pypiswasta
Group=pypiswasta
WorkingDirectory=/opt/pypiswasta
ExecStart=/opt/pypiswasta/.venv/bin/pypiswasta serve --config /etc/pypiswasta/config.toml
Restart=on-failure
UMask=0077

[Install]
WantedBy=multi-user.target
```

Start it and check that it responds:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now pypiswasta
curl --fail http://127.0.0.1:8374/healthz
```

Complete setup through the tunnel described above. View logs with
`sudo journalctl -u pypiswasta -f`.

### Put HTTPS in front

For an existing Caddy installation, add a site with your domain:

```caddyfile
pypi.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8374
}
```

Point DNS at the server and allow HTTP/HTTPS traffic to Caddy, then validate and
reload its configuration. See [Caddy's reverse proxy guide](https://caddyserver.com/docs/quick-starts/reverse-proxy).

Keep all downloads routed through the application: **do not expose the data
directory as a static website**. If the proxy has an upload-size limit, allow
room for both the configured artifact limit and multipart metadata (up to an
additional 8.25 MiB). Preserve the application's cache headers for private content.

## Publish packages

Run this from the Python project you want to publish, replacing the URL and
credentials with your deployment's values:

```sh
uv build
export UV_PUBLISH_USERNAME=publisher
export UV_PUBLISH_PASSWORD='<upload password from your secret store>'
uv publish --publish-url https://pypi.example.com/legacy/ dist/*
```

Keep real passwords out of committed files and shell history. CI can supply
these environment variables from its secret store. For local use, configure
credentials through your client's credential store. See [uv's publishing guide](https://docs.astral.sh/uv/guides/publish/).

Twine is also supported; it accepts `TWINE_USERNAME` and `TWINE_PASSWORD`:

```sh
twine upload --repository-url https://pypi.example.com/legacy/ dist/*
```

Wheels (`.whl`) and source distributions (`.tar.gz`) are supported. Each HTTP
request accepts one distribution; these clients upload the files separately.

**New projects are private by default.** On the server, make a project public:

```sh
pypiswasta package visibility my-package public --config /etc/pypiswasta/config.toml
```

Use `.venv/bin/pypiswasta` or activate the server's virtual environment for CLI
commands, and run them as the service account. For the local quick start, use
`--config ./config.toml` instead. Package visibility changes take effect without
a restart and apply to every version of the project.

### Replacing an uploaded file

Uploading the same filename with identical bytes succeeds without changing the
file. Different bytes replace it by default; set `repository.allow_overwrite = false`
to reject replacements with `409 Conflict`.

After a replacement, refresh affected lockfiles and client caches as needed
(for example, `uv cache clean my-package`). Avoid `uv publish --check-url` when
replacing a file: it skips filenames already present on the index.

### Yank a broken release

On the server, mark an entire version as yanked, with an optional reason:

```sh
pypiswasta package yank my-package 1.2.3 --reason "Broken imports" --config /etc/pypiswasta/config.toml
pypiswasta package unyank my-package 1.2.3 --config /etc/pypiswasta/config.toml
```

Yanking and unyanking are CLI-only operations and take effect without a service
restart. They cover every wheel and sdist for that version, including files added
later or replaced after the yank. Equivalent versions such as `1.2` and `1.2.0`
share the same state; local versions such as `1.2+cpu` remain separate.

When browsing a project's Simple index page, yanked files display a bold
`(YANKED)` marker before their filenames.

Yanked releases stay listed and downloadable, with a marker telling installers
to skip them during normal version selection. Exact pins such as
`my-package==1.2.3` may still install them with a warning. Yanking does not remove
files or change access permissions. See the [file-yanking specification](https://packaging.python.org/en/latest/specifications/file-yanking/).

### Use tokens for automation

Give each publisher its own upload token so it can be revoked independently:

```sh
pypiswasta token create github-actions --role upload --config /etc/pypiswasta/config.toml
```

Save the token when it is displayed, then **restart the service**. Use `__token__`
as the publishing username and the token as the password. Reader tokens can be
created with `--role reader` for private installs.

```sh
pypiswasta token list --config /etc/pypiswasta/config.toml
pypiswasta token remove github-actions --config /etc/pypiswasta/config.toml
```

Restart after creating or removing tokens; changes become active when the
service reloads its credentials at startup.

## Install packages

For public packages, inside a virtual environment:

```sh
uv pip install --default-index https://pypi.example.com/simple/ my-package
```

With pip, use `pip install --index-url https://pypi.example.com/simple/ my-package`.
These commands use this repository as the default index; it does not proxy PyPI.

For a uv project that uses private packages alongside dependencies from PyPI, add
this to the consuming project's `pyproject.toml`:

```toml
[[tool.uv.index]]
name = "personal"
url = "https://pypi.example.com/private/simple/"
authenticate = "always"
explicit = true

[tool.uv.sources]
my-package = { index = "personal" }
```

Supply the credentials and add the package to the project's dependencies:

```sh
export UV_INDEX_PERSONAL_USERNAME=reader
export UV_INDEX_PERSONAL_PASSWORD='<reader password from your secret store>'
uv add my-package
```

If it is already a dependency, run `uv sync` instead.

The private endpoint includes both public and private projects and requires
`reader` or `admin` access. An upload credential cannot install private packages.
Pinning your internal packages to the named index keeps their resolution tied to
your repository. See [uv's index configuration guide](https://docs.astral.sh/uv/concepts/indexes/)
for credential providers and index behavior.

## Find packages

Open `https://pypi.example.com/search?q=my-package` to search project names.
Anonymous visitors see public projects; reader or administrator credentials
also reveal private projects.

Clients supporting legacy XML-RPC search can search names and summaries:

```sh
pip search --index https://pypi.example.com/pypi my-package
```

Use `/pypi` for this command, rather than `/simple/`.

## Configuration and maintenance

The full configuration is in [config.example.toml](config.example.toml).
Storage paths must be absolute. The main defaults are:

| Setting | Default | Purpose |
| --- | --- | --- |
| `server.host` / `server.port` | `127.0.0.1` / `8374` | Local listening address |
| `server.workers` | `1` | Keep one worker for a simple deployment |
| `storage.max_upload_size_mb` | `1024` | Maximum artifact size in MiB |
| `repository.default_visibility` | `"private"` | Visibility of new projects |
| `repository.allow_overwrite` | `true` | Allow different bytes for an existing filename |

Optional `[branding]` settings change the title, tagline, colors and footer.
The footer accepts trusted HTML. Credentials live in SQLite, not in TOML;
`[authentication]` configuration is unsupported.

### Manage credentials

```sh
pypiswasta auth list --config /etc/pypiswasta/config.toml
pypiswasta auth reset-password reader --config /etc/pypiswasta/config.toml
pypiswasta auth set-username upload publisher --config /etc/pypiswasta/config.toml
pypiswasta auth set-admin-password --config /etc/pypiswasta/config.toml
```

`reset-password` works for all three roles and displays a new password once;
`rotate` is an alias. `set-admin-password` prompts for a password you choose.
Restart after changing credentials, tokens or configuration:

```sh
sudo systemctl restart pypiswasta
```

### Reclaim storage

Deleting or replacing a package leaves unreferenced files on disk until garbage
collection. Preview a collection before running it:

```sh
pypiswasta package list --config /etc/pypiswasta/config.toml
pypiswasta gc --dry-run --config /etc/pypiswasta/config.toml
pypiswasta gc --config /etc/pypiswasta/config.toml
```

GC keeps files younger than one hour by default. To delete a project containing
published files, use `pypiswasta package delete my-package --force --config /etc/pypiswasta/config.toml`.
This removes its records; GC later reclaims the files. Making a public project
private prevents further public downloads from the origin, but cannot retract
copies clients or caches already hold.

### Back up and upgrade

For a simple consistent backup, stop the service and avoid CLI writes or garbage
collection while copying the configuration, database and entire data directory.
Include the database's WAL files if present, and include its parent directory
when the database is stored outside `data_dir`. Protect the backup: the database
contains credential hashes and private package records. Restart after copying.

Before upgrading, take a backup. Stop the service, update the checkout, run
`uv sync --locked --no-dev` there, then restart. Schema migrations run
automatically at startup; restore the matching database, data and application
revision together if you need to roll back.

## Project documentation

- [AGENTS.md](AGENTS.md): development commands, code map and implementation constraints.
- [Implementation notes](docs/implementation.md): original design and later implementation decisions.
- [Security review](docs/security-review.md): recorded findings, fixes and verification evidence.

PyPISwasta does not mirror PyPI, build packages, provide signing/attestations,
offer per-version visibility, or provide multiple accounts per role or a browser
administration dashboard. Use tokens for individual automation credentials.
