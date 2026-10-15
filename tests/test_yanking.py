"""CLI yanks must reach installers without removing or exposing artifact bytes."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from conftest import build_sdist, build_wheel, sdist_filename, wheel_filename
from pypiswasta.app import create_app
from pypiswasta.cli import main

JSON_MEDIA = "application/vnd.pypi.simple.v1+json"


class FileLinks(HTMLParser):
    def __init__(self, document: str) -> None:
        super().__init__()
        self.links: list[dict[str, str | None]] = []
        self.feed(document)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.links.append(dict(attrs))


def publish(instance, version="1.0.0", *, sdist=False, summary=None):
    builder = build_sdist if sdist else build_wheel
    filename = sdist_filename if sdist else wheel_filename
    response = instance.upload(builder("demo", version, summary=summary), filename("demo", version))
    assert response.status_code == 200, response.text
    return response


def files(instance, *, private=True):
    prefix = "/private" if private else ""
    response = instance.client.get(
        f"{prefix}/simple/demo/",
        headers={"Accept": JSON_MEDIA},
        auth=instance.auth("reader") if private else None,
    )
    assert response.status_code == 200, response.text
    return response.json()["files"]


def run(config_path, action, version="1.0.0", *extra):
    return main(["package", action, "demo", version, *extra, "--config", str(config_path)])


@pytest.mark.parametrize("private", [False, True], ids=["public", "private"])
@pytest.mark.parametrize("reason", [None, "", 'Broken "imports" & <tags> — café'])
def test_yank_and_unyank_reach_both_index_formats(instance, config_path, private, reason) -> None:
    for version in ("1.0.0", "2.0.0"):
        publish(instance, version)
        publish(instance, version, sdist=True)
    if not private:
        assert (
            instance.client.patch(
                "/api/admin/packages/demo", json={"private": False}, auth=instance.auth("admin")
            ).status_code
            == 200
        )

    before = files(instance, private=private)
    assert all("yanked" not in entry for entry in before)
    extra = [] if reason is None else ["--reason", reason]
    assert run(config_path, "yank", "2.0.0", *extra) == 0

    entries = files(instance, private=private)
    assert len(entries) == 4
    for entry in entries:
        if "-2.0.0" in entry["filename"]:
            assert entry["yanked"] == (reason or True)
        else:
            assert "yanked" not in entry

    prefix = "/private" if private else ""
    auth = instance.auth("reader") if private else None
    html = instance.client.get(f"{prefix}/simple/demo/", auth=auth)
    assert html.headers["cache-control"] == ("private, no-store" if private else "no-cache, must-revalidate")
    links = FileLinks(html.text).links
    assert len(links) == 4
    for link in links:
        if "-2.0.0" in link["href"]:  # type: ignore
            assert link["data-yanked"] == (reason or "")
        else:
            assert "data-yanked" not in link
    for entry in entries:
        assert instance.client.get(entry["url"], auth=auth).status_code == 200
        assert instance.client.get(entry["url"] + ".metadata", auth=auth).status_code == 200

    assert run(config_path, "unyank", "2.0.0") == 0
    assert files(instance, private=private) == before
    unyanked_links = FileLinks(instance.client.get(f"{prefix}/simple/demo/", auth=auth).text).links
    assert all("data-yanked" not in link for link in unyanked_links)


def test_yanking_preserves_private_access_rules(instance, config_path) -> None:
    publish(instance)
    assert run(config_path, "yank", "1.0.0", "--reason", "private reason") == 0
    entry = files(instance)[0]
    assert instance.client.get("/simple/demo/").status_code == 404
    assert "demo" not in instance.client.get("/simple/").text
    assert instance.client.get("/private/simple/demo/").status_code == 401
    assert instance.client.get("/private/simple/demo/", auth=instance.auth("upload")).status_code == 403
    assert instance.client.get(entry["url"]).status_code == 401
    assert instance.client.get(entry["url"].removeprefix("/private")).status_code == 404


@pytest.mark.parametrize(
    ("uploaded", "requested"),
    [("1.2.0", "v1.2"), ("1.2", "1.2.0"), ("1.0rc1", "1.0RC01"), ("1.0+cpu", "1.0.0+CPU")],
)
def test_yanking_uses_pep440_version_equivalence(instance, config_path, uploaded, requested) -> None:
    publish(instance, uploaded)
    assert run(config_path, "yank", requested) == 0
    assert files(instance)[0]["yanked"] is True
    assert run(config_path, "unyank", requested) == 0
    assert "yanked" not in files(instance)[0]


def test_yank_covers_equivalent_versions_but_not_other_local_versions(instance, config_path) -> None:
    publish(instance, "1.0")
    publish(instance, "1.0.0", sdist=True)
    publish(instance, "1.0+cpu")
    publish(instance, "1.0+gpu")
    assert run(config_path, "yank", "1.0.0") == 0
    for entry in files(instance):
        assert entry.get("yanked", False) is ("+" not in entry["filename"])
    assert run(config_path, "yank", "1.0+cpu", "--reason", "CPU build broken") == 0
    assert run(config_path, "unyank", "1.0") == 0
    for entry in files(instance):
        assert entry.get("yanked", False) == ("CPU build broken" if "+cpu" in entry["filename"] else False)


def test_new_files_and_replacements_cannot_clear_a_yank(instance, config_path) -> None:
    original = publish(instance)
    assert run(config_path, "yank", "1.0", "--reason", "Broken release") == 0
    assert publish(instance).json()["sha256"] == original.json()["sha256"]
    assert publish(instance, summary="Replacement").json()["sha256"] != original.json()["sha256"]
    publish(instance, "1.0", sdist=True)
    extra = instance.upload(
        build_wheel("demo", "1.0.0"),
        "demo-1.0.0-1-py3-none-any.whl",
        form={"yanked": "false", "yanked_reason": ""},
    )
    assert extra.status_code == 200, extra.text
    assert len(files(instance)) == 3
    assert all(entry["yanked"] == "Broken release" for entry in files(instance))


@pytest.mark.parametrize("unyank_before_upload", [False, True])
def test_yank_survives_removing_all_files(instance, config_path, unyank_before_upload) -> None:
    publish(instance)
    assert run(config_path, "yank") == 0
    entry = files(instance)[0]
    response = instance.client.delete(
        f"/api/admin/packages/demo/files/{entry['filename']}", auth=instance.auth("admin")
    )
    assert response.status_code == 204
    assert instance.client.get("/private/simple/demo/", auth=instance.auth("reader")).status_code == 404
    if unyank_before_upload:
        assert run(config_path, "unyank") == 0
    publish(instance, sdist=True)
    assert files(instance)[0].get("yanked", False) is not unyank_before_upload


def test_repeated_yanks_update_reason_and_unyanking_is_idempotent(instance, config_path) -> None:
    publish(instance)
    assert run(config_path, "unyank") == 0
    for reason in ("First reason", "Updated reason", ""):
        assert run(config_path, "yank", "1.0.0", "--reason", reason) == 0
        assert files(instance)[0]["yanked"] == (reason or True)
    assert run(config_path, "unyank") == 0
    assert run(config_path, "unyank") == 0
    assert "yanked" not in files(instance)[0]


@pytest.mark.parametrize("action", ["yank", "unyank"])
@pytest.mark.parametrize(
    ("name", "version", "error"),
    [
        ("missing", "1.0.0", "unknown project"),
        ("demo", "9.0.0", "unknown version"),
        ("demo", "invalid", "invalid version"),
    ],
)
def test_invalid_yank_commands_fail_without_changing_releases(
    instance, config_path, capsys, action, name, version, error
):
    publish(instance)
    assert main(["package", action, name, version, "--config", str(config_path)]) == 1
    assert error in capsys.readouterr().err
    assert "yanked" not in files(instance)[0]


def test_yank_persists_across_restart_and_is_removed_with_project(instance, config_path) -> None:
    publish(instance)
    assert run(config_path, "yank", "1.0.0", "--reason", "Broken release") == 0
    with TestClient(create_app(config_path)) as client:
        response = client.get("/private/simple/demo/", headers={"Accept": JSON_MEDIA}, auth=instance.auth("reader"))
        assert response.json()["files"][0]["yanked"] == "Broken release"
    assert main(["package", "delete", "demo", "--force", "--config", str(config_path)]) == 0
    publish(instance)
    assert "yanked" not in files(instance)[0]
