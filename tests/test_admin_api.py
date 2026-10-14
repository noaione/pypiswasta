from __future__ import annotations

from conftest import build_wheel

FILENAME = "demo-1.0.0-py3-none-any.whl"


def _publish(instance, name: str = "demo", version: str = "1.0.0") -> None:
    from conftest import wheel_filename

    response = instance.upload(build_wheel(name, version), wheel_filename(name, version))
    assert response.status_code == 200, response.text


def test_package_listing_includes_files(instance) -> None:
    _publish(instance)

    response = instance.client.get("/api/admin/packages", auth=instance.auth("admin"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"

    packages = response.json()["packages"]
    assert len(packages) == 1
    package = packages[0]
    assert package["name"] == "demo"
    assert package["private"] is True
    assert len(package["files"]) == 1

    file_entry = package["files"][0]
    assert file_entry["filename"] == FILENAME
    assert file_entry["version"] == "1.0.0"
    assert file_entry["requires_python"] == ">=3.9"
    assert len(file_entry["sha256"]) == 64
    assert len(file_entry["metadata_sha256"]) == 64


def test_package_listing_filters(instance) -> None:
    _publish(instance, "alpha")
    _publish(instance, "beta")
    instance.client.patch("/api/admin/packages/beta", json={"private": False}, auth=instance.auth("admin"))

    auth = instance.auth("admin")
    all_names = [
        package["name"] for package in instance.client.get("/api/admin/packages", auth=auth).json()["packages"]
    ]
    assert all_names == ["alpha", "beta"]

    public = instance.client.get("/api/admin/packages?visibility=public", auth=auth).json()["packages"]
    assert [package["name"] for package in public] == ["beta"]

    matched = instance.client.get("/api/admin/packages?q=alp", auth=auth).json()["packages"]
    assert [package["name"] for package in matched] == ["alpha"]

    assert instance.client.get("/api/admin/packages?visibility=weird", auth=auth).status_code == 400


def test_patch_visibility(instance) -> None:
    _publish(instance)
    auth = instance.auth("admin")

    response = instance.client.patch("/api/admin/packages/demo", json={"visibility": "public"}, auth=auth)
    assert response.status_code == 200
    assert response.json() == {"name": "demo", "private": False}

    public_root = instance.client.get("/simple/")
    assert "demo" in public_root.text


def test_patch_validation(instance) -> None:
    _publish(instance)
    auth = instance.auth("admin")

    assert instance.client.patch("/api/admin/packages/demo", json={}, auth=auth).status_code == 400
    assert instance.client.patch("/api/admin/packages/demo", json={"private": "yes"}, auth=auth).status_code == 400
    assert (
        instance.client.patch(
            "/api/admin/packages/demo",
            json={"private": True, "visibility": "public"},
            auth=auth,
        ).status_code
        == 400
    )
    assert instance.client.patch("/api/admin/packages/missing", json={"private": False}, auth=auth).status_code == 404

    bad_body = instance.client.patch(
        "/api/admin/packages/demo",
        content=b"not json",
        headers={"Content-Type": "application/json"},
        auth=auth,
    )
    assert bad_body.status_code == 400


def test_delete_single_file(instance) -> None:
    _publish(instance)
    auth = instance.auth("admin")

    files = instance.client.app.state.store
    response = instance.client.delete(f"/api/admin/packages/demo/files/{FILENAME}", auth=auth)
    assert response.status_code == 204
    assert files  # sanity: store object still reachable

    assert instance.client.get("/private/simple/demo/", auth=instance.auth("reader")).status_code == 404
    packages = instance.client.get("/api/admin/packages", auth=auth).json()["packages"]
    assert packages[0]["files"] == []

    # Second delete of the same file is a 404.
    assert instance.client.delete(f"/api/admin/packages/demo/files/{FILENAME}", auth=auth).status_code == 404


def test_delete_project_requires_empty_project(instance) -> None:
    _publish(instance)
    auth = instance.auth("admin")

    conflict = instance.client.delete("/api/admin/packages/demo", auth=auth)
    assert conflict.status_code == 409
    assert "delete them first" in conflict.json()["detail"]

    instance.client.delete(f"/api/admin/packages/demo/files/{FILENAME}", auth=auth)
    assert instance.client.delete("/api/admin/packages/demo", auth=auth).status_code == 204
    assert instance.client.get("/api/admin/packages", auth=auth).json()["packages"] == []
    assert instance.client.delete("/api/admin/packages/demo", auth=auth).status_code == 404


def test_admin_endpoints_require_admin_role(instance) -> None:
    for role in ("reader", "upload"):
        response = instance.client.get("/api/admin/packages", auth=instance.auth(role))
        assert response.status_code == 403, role

    assert instance.client.get("/api/admin/packages").status_code == 401
    assert instance.client.patch("/api/admin/packages/demo", json={"private": False}).status_code == 401
