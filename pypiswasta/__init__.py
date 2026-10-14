"""PyPISwasta — a minimal, wheel-only Python package index.

Public packages are readable by anyone, private packages require a shared
`reader` (or `admin`) credential. Publishing requires the `upload`
(or `admin`) credential. Credentials live in SQLite, never in the TOML
configuration file.
"""

__version__ = "0.1.0"
