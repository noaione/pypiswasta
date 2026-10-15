from __future__ import annotations

import hashlib
import json

from conftest import build_wheel, wheel_filename, wheel_metadata_bytes
from pypiswasta import simple, web

JSON_MEDIA = "application/vnd.pypi.simple.v1+json"
HTML_MEDIA = "application/vnd.pypi.simple.v1+html"


def _publish_public(instance, name: str = "demo", version: str = "1.0.0", **kwargs):
    """Upload a wheel and flip the project to public. Returns (wheel, digest)."""
    wheel = build_wheel(name, version, **kwargs)
    response = instance.upload(wheel, wheel_filename(name, version))
    assert response.status_code == 200, response.text
    digest = response.json()["sha256"]
    patch = instance.client.patch(
        f"/api/admin/packages/{name}",
        json={"private": False},
        auth=instance.auth("admin"),
    )
    assert patch.status_code == 200, patch.text
    return wheel, digest


def _upload_private(instance, name: str = "secret", version: str = "1.0.0"):
    wheel = build_wheel(name, version)
    response = instance.upload(wheel, wheel_filename(name, version))
    assert response.status_code == 200, response.text
    return wheel, response.json()["sha256"]


# --------------------------------------------------------------------------- #
# index listings
# --------------------------------------------------------------------------- #


def test_empty_root_index_is_valid_html(instance) -> None:
    response = instance.client.get("/simple/")
    assert response.status_code == 200
    # Browsers (and httpx's default `Accept: */*`) must get renderable HTML.
    assert response.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in response.text
    assert 'meta name="pypi:repository-version" content="1.1"' in response.text
    assert response.headers["vary"] == "Accept"
    assert response.headers["cache-control"] == "no-cache, must-revalidate"


def test_private_project_hidden_from_public_index(instance) -> None:
    _upload_private(instance, "secret")

    root = instance.client.get("/simple/")
    assert "secret" not in root.text
    assert instance.client.get("/simple/secret/").status_code == 404


def test_reader_sees_every_project(instance) -> None:
    _upload_private(instance, "secret")
    _publish_public(instance, "demo")

    response = instance.client.get("/private/simple/", auth=instance.auth("reader"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert 'href="/private/simple/secret/"' in response.text
    assert 'href="/private/simple/demo/"' in response.text


def test_public_index_only_lists_public_projects(instance) -> None:
    _upload_private(instance, "secret")
    _publish_public(instance, "demo")

    root = instance.client.get("/simple/")
    assert "demo" in root.text
    assert "secret" not in root.text


# --------------------------------------------------------------------------- #
# auth on index and file endpoints
# --------------------------------------------------------------------------- #


def test_private_endpoints_require_credentials(instance) -> None:
    response = instance.client.get("/private/simple/")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="PyPISwasta"'

    assert instance.client.get("/private/simple/", auth=("reader", "nope")).status_code == 401
    unknown = "/private/files/" + "0" * 64 + "/demo-1.0.0-py3-none-any.whl"
    assert instance.client.get(unknown, auth=("reader", "nope")).status_code == 401


def test_upload_role_cannot_use_reader_endpoints(instance) -> None:
    response = instance.client.get("/private/simple/", auth=instance.auth("upload"))
    assert response.status_code == 403


def test_malformed_authorization_header_is_rejected(instance) -> None:
    response = instance.client.get("/private/simple/", headers={"Authorization": "Basic not-base64!!"})
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# JSON / HTML payloads
# --------------------------------------------------------------------------- #


def test_project_json_has_full_metadata(instance) -> None:
    wheel, digest = _publish_public(instance, "demo", "1.2.3", requires_python=">=3.10")

    response = instance.client.get("/simple/demo/", headers={"Accept": JSON_MEDIA})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(JSON_MEDIA)

    payload = json.loads(response.text)
    assert payload["meta"] == {"api-version": "1.1"}
    assert payload["name"] == "demo"
    assert payload["versions"] == ["1.2.3"]

    entry = payload["files"][0]
    assert entry["filename"] == "demo-1.2.3-py3-none-any.whl"
    assert entry["url"] == f"/files/{digest}/demo-1.2.3-py3-none-any.whl"
    assert entry["hashes"]["sha256"] == digest
    assert digest == hashlib.sha256(wheel).hexdigest()
    assert entry["size"] == len(wheel)
    assert entry["requires-python"] == ">=3.10"
    assert entry["core-metadata"]["sha256"] == hashlib.sha256(wheel_metadata_bytes(wheel)).hexdigest()


def test_project_html_uses_root_absolute_urls(instance) -> None:
    _wheel, digest = _publish_public(instance, "demo", "1.0.0")

    response = instance.client.get("/simple/demo/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<meta name="pypi:repository-version" content="1.1">' in response.text
    assert f'href="/files/{digest}/demo-1.0.0-py3-none-any.whl#sha256={digest}"' in response.text
    assert ">demo-1.0.0-py3-none-any.whl</a>" in response.text
    assert 'data-requires-python="&gt;=3.9"' in response.text
    assert 'data-core-metadata="sha256=' in response.text


def test_private_project_html_links_stay_in_private_namespace(instance) -> None:
    _wheel, digest = _upload_private(instance, "secret")

    response = instance.client.get("/private/simple/secret/", auth=instance.auth("reader"))
    assert response.status_code == 200
    assert f"/private/files/{digest}/secret-1.0.0-py3-none-any.whl" in response.text
    assert f'href="/files/{digest}' not in response.text


def test_content_negotiation(instance) -> None:
    _publish_public(instance)

    json_response = instance.client.get("/simple/", headers={"Accept": f"{JSON_MEDIA};q=0.9, text/html;q=0.8"})
    assert json_response.headers["content-type"].startswith(JSON_MEDIA)
    assert json_response.headers["vary"] == "Accept"

    # A real browser Accept header must yield *renderable* HTML, not the vendor
    # media type, which browsers treat as a download.
    browser = instance.client.get(
        "/simple/",
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
    )
    assert browser.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in browser.text

    no_header = instance.client.get("/simple/", headers={"Accept": ""})
    assert no_header.headers["content-type"].startswith("text/html")

    wildcard = instance.client.get("/simple/", headers={"Accept": "*/*"})
    assert wildcard.headers["content-type"].startswith("text/html")


def test_explicit_media_types_are_echoed(instance) -> None:
    _publish_public(instance, "demo")

    vendor_html = instance.client.get("/simple/", headers={"Accept": HTML_MEDIA})
    assert vendor_html.headers["content-type"] == f"{HTML_MEDIA}; charset=utf-8"

    project = instance.client.get("/simple/demo/", headers={"Accept": HTML_MEDIA})
    assert project.headers["content-type"] == f"{HTML_MEDIA}; charset=utf-8"

    plain_json = instance.client.get("/simple/", headers={"Accept": "application/json"})
    assert plain_json.headers["content-type"].startswith("application/json")

    # text/html is not acceptable when the client only allows application/*.
    app_wildcard = instance.client.get("/simple/", headers={"Accept": "application/*"})
    assert app_wildcard.headers["content-type"] == f"{HTML_MEDIA}; charset=utf-8"

    vendor_json = instance.client.get("/simple/", headers={"Accept": JSON_MEDIA})
    assert vendor_json.headers["content-type"].startswith(JSON_MEDIA)


def test_root_json_lists_project_names(instance) -> None:
    _publish_public(instance, "demo")
    response = instance.client.get("/simple/", headers={"Accept": JSON_MEDIA})
    payload = json.loads(response.text)
    assert payload == {"meta": {"api-version": "1.1"}, "projects": [{"name": "demo"}]}


def test_multiple_versions_are_sorted(instance) -> None:
    _publish_public(instance, "demo", "1.0.0")
    for version in ("2.0.0", "1.5.0"):
        response = instance.upload(build_wheel("demo", version), wheel_filename("demo", version))
        assert response.status_code == 200

    payload = json.loads(instance.client.get("/simple/demo/", headers={"Accept": JSON_MEDIA}).text)
    assert payload["versions"] == ["1.0.0", "1.5.0", "2.0.0"]
    assert len(payload["files"]) == 3


# --------------------------------------------------------------------------- #
# downloads
# --------------------------------------------------------------------------- #


def test_public_download_and_metadata_sidecar(instance) -> None:
    wheel, digest = _publish_public(instance, "demo", "1.0.0")
    filename = "demo-1.0.0-py3-none-any.whl"

    download = instance.client.get(f"/files/{digest}/{filename}")
    assert download.status_code == 200
    assert download.content == wheel
    assert download.headers["content-type"] == "application/octet-stream"
    assert filename in download.headers["content-disposition"]

    sidecar = instance.client.get(f"/files/{digest}/{filename}.metadata")
    assert sidecar.status_code == 200
    assert sidecar.content == wheel_metadata_bytes(wheel)


def test_private_wheel_needs_reader_credentials(instance) -> None:
    wheel, digest = _upload_private(instance, "secret")
    filename = "secret-1.0.0-py3-none-any.whl"

    # The public namespace never exposes a private artifact.
    assert instance.client.get(f"/files/{digest}/{filename}").status_code == 404
    assert instance.client.get(f"/files/{digest}/{filename}", auth=instance.auth("reader")).status_code == 404
    # The private namespace requires credentials.
    assert instance.client.get(f"/private/files/{digest}/{filename}").status_code == 401
    allowed = instance.client.get(f"/private/files/{digest}/{filename}", auth=instance.auth("reader"))
    assert allowed.status_code == 200
    assert allowed.content == wheel
    assert allowed.headers["cache-control"] == "private, no-store"


def test_visibility_flip_hides_previously_public_bytes(instance) -> None:
    _wheel, digest = _publish_public(instance, "demo", "1.0.0")
    filename = "demo-1.0.0-py3-none-any.whl"
    url = f"/files/{digest}/{filename}"

    assert instance.client.get(url).status_code == 200

    flip = instance.client.patch("/api/admin/packages/demo", json={"private": True}, auth=instance.auth("admin"))
    assert flip.status_code == 200

    assert instance.client.get(url).status_code == 404
    assert instance.client.get(f"{url}.metadata").status_code == 404
    assert "demo" not in instance.client.get("/simple/").text


def test_unknown_digest_and_unknown_project(instance) -> None:
    _publish_public(instance, "demo", "1.0.0")
    assert instance.client.get("/files/" + "0" * 64 + "/demo-1.0.0-py3-none-any.whl").status_code == 404
    assert instance.client.get("/files/notahex/demo-1.0.0-py3-none-any.whl").status_code == 404
    assert instance.client.get("/simple/missing/").status_code == 404


def test_head_requests(instance) -> None:
    _wheel, digest = _publish_public(instance, "demo", "1.0.0")
    filename = "demo-1.0.0-py3-none-any.whl"

    index_head = instance.client.head("/simple/")
    assert index_head.status_code == 200
    assert index_head.content == b""

    file_head = instance.client.head(f"/files/{digest}/{filename}")
    assert file_head.status_code == 200
    assert file_head.content == b""
    assert int(file_head.headers["content-length"]) > 0


def test_missing_trailing_slash_redirects(instance) -> None:
    _publish_public(instance, "demo", "1.0.0")

    root = instance.client.get("/simple", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/simple/"

    project = instance.client.get("/simple/demo", follow_redirects=False)
    assert project.status_code == 307
    assert project.headers["location"] == "/simple/demo/"

    private = instance.client.get("/private/simple", follow_redirects=False)
    assert private.status_code == 307
    assert private.headers["location"] == "/private/simple/"

    # Following the redirect keeps the request inside the same namespace.
    followed = instance.client.get("/private/simple/demo", auth=instance.auth("reader"))
    assert followed.status_code == 200


# --------------------------------------------------------------------------- #
# URL encoding
# --------------------------------------------------------------------------- #


def test_index_links_percent_encode_the_name() -> None:
    """HTML escaping alone leaves ``#``, ``?`` and ``%`` meaning something else."""
    assert web.project_url("bad#name") == "/simple/bad%23name/"
    assert web.project_url("bad?name", private=True) == "/private/simple/bad%3Fname/"
    assert 'href="/simple/bad%23name/"' in simple.render_root_html(["bad#name"])


def test_file_urls_encode_reserved_characters_and_still_download(instance) -> None:
    """A local version puts ``+`` in the filename, so the URL has to encode it."""
    wheel = build_wheel("demo", "1.0+local")
    filename = "demo-1.0+local-py3-none-any.whl"

    response = instance.upload(wheel, filename)
    assert response.status_code == 200, response.text
    digest = response.json()["sha256"]

    index = instance.client.get("/private/simple/demo/", headers={"Accept": JSON_MEDIA}, auth=instance.auth("reader"))
    url = json.loads(index.text)["files"][0]["url"]
    assert url == f"/private/files/{digest}/demo-1.0%2Blocal-py3-none-any.whl"

    download = instance.client.get(url, auth=instance.auth("reader"))
    assert download.status_code == 200
    assert download.content == wheel
