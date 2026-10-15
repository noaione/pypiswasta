"""Catalog search: matching, visibility scoping and escaping."""

from __future__ import annotations

from conftest import build_sdist, build_wheel, sdist_filename, wheel_filename


def publish(instance, name: str, version: str = "1.0.0", *, public: bool = False) -> None:
    """Publish a wheel for ``name`` and optionally make it public."""
    response = instance.upload(build_wheel(name, version), wheel_filename(name, version))
    assert response.status_code == 200, response.text
    if public:
        promoted = instance.client.patch(
            f"/api/admin/packages/{name}",
            json={"visibility": "public"},
            auth=instance.auth("admin"),
        )
        assert promoted.status_code == 200


def test_search_form_is_on_the_landing_page(instance) -> None:
    page = instance.client.get("/").text
    assert '<form method="get" action="/search"' in page
    assert 'name="q"' in page


def test_search_without_a_query_lists_nothing(instance) -> None:
    publish(instance, "demo", public=True)

    page = instance.client.get("/search").text
    assert "Enter part of a project name" in page
    assert "/simple/demo/" not in page


def test_search_finds_a_public_project(instance) -> None:
    publish(instance, "demo", public=True)

    page = instance.client.get("/search", params={"q": "dem"}).text
    assert "1 match(es)" in page
    assert '<a href="/simple/demo/">demo</a>' in page


def test_search_is_case_and_separator_insensitive(instance) -> None:
    publish(instance, "My-Package", public=True)

    for term in ("My-Package", "my_package", "MY.PACKAGE", "my-package"):
        page = instance.client.get("/search", params={"q": term}).text
        assert "1 match(es)" in page, term


def test_search_matches_a_substring(instance) -> None:
    publish(instance, "alpha-tools", public=True)
    publish(instance, "beta-tools", public=True)
    publish(instance, "other", public=True)

    page = instance.client.get("/search", params={"q": "tools"}).text
    assert "2 match(es)" in page
    assert "/simple/alpha-tools/" in page
    assert "/simple/beta-tools/" in page
    assert "/simple/other/" not in page


def test_search_reports_no_matches(instance) -> None:
    publish(instance, "demo", public=True)

    page = instance.client.get("/search", params={"q": "absent"}).text
    assert "No project matches" in page


def test_search_shows_the_file_count(instance) -> None:
    publish(instance, "demo", "1.0.0", public=True)
    publish(instance, "demo", "2.0.0")

    page = instance.client.get("/search", params={"q": "demo"}).text
    assert "<td>2</td>" in page


# --------------------------------------------------------------------------- #
# visibility
# --------------------------------------------------------------------------- #


def test_search_hides_private_projects_from_anonymous_callers(instance) -> None:
    publish(instance, "secret", public=False)

    page = instance.client.get("/search", params={"q": "secret"}).text
    assert "No project matches" in page
    assert "Showing public projects only" in page


def test_search_includes_private_projects_for_reader_credentials(instance) -> None:
    publish(instance, "secret", public=False)

    page = instance.client.get("/search", params={"q": "secret"}, auth=instance.auth("reader")).text
    assert "1 match(es)" in page
    assert '<a href="/private/simple/secret/">secret</a>' in page
    assert "You are authenticated" in page


def test_search_accepts_the_upload_role_for_the_reader_view(instance) -> None:
    """``upload`` may publish but must not read private projects, so it is 403."""
    publish(instance, "secret", public=False)

    response = instance.client.get("/search", params={"q": "secret"}, auth=instance.auth("upload"))
    assert response.status_code == 403


def test_search_rejects_a_wrong_credential_instead_of_downgrading(instance) -> None:
    response = instance.client.get("/search", params={"q": "demo"}, auth=("reader", "wrong"))
    assert response.status_code == 401


def test_search_only_lists_installable_projects(instance) -> None:
    """A project with no files left is hidden, matching the index behaviour."""
    publish(instance, "demo", public=True)
    deleted = instance.client.delete(
        "/api/admin/packages/demo/files/demo-1.0.0-py3-none-any.whl",
        auth=instance.auth("admin"),
    )
    assert deleted.status_code == 204

    page = instance.client.get("/search", params={"q": "demo"}).text
    assert "No project matches" in page


# --------------------------------------------------------------------------- #
# escaping
# --------------------------------------------------------------------------- #


def test_search_query_is_html_escaped(instance) -> None:
    page = instance.client.get("/search", params={"q": "<script>alert(1)</script>"}).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_wildcards_in_the_query_do_not_match_everything(instance) -> None:
    """``%`` must be a literal, not a ``LIKE`` wildcard."""
    publish(instance, "demo", public=True)

    for wildcard in ("%", "_", "%%"):
        page = instance.client.get("/search", params={"q": wildcard}).text
        assert "No project matches" in page, wildcard


# --------------------------------------------------------------------------- #
# both distribution formats
# --------------------------------------------------------------------------- #


def test_search_counts_sdists(instance) -> None:
    assert instance.upload(build_sdist("demo", "1.0.0"), sdist_filename("demo", "1.0.0")).status_code == 200
    assert (
        instance.client.patch(
            "/api/admin/packages/demo",
            json={"visibility": "public"},
            auth=instance.auth("admin"),
        ).status_code
        == 200
    )

    page = instance.client.get("/search", params={"q": "demo"}).text
    assert "1 match(es)" in page


# --------------------------------------------------------------------------- #
# cache protections
# --------------------------------------------------------------------------- #


def test_search_response_is_never_cacheable(instance) -> None:
    """Whoever asked decides the results, so no cache may keep the response."""
    publish(instance, "secret", public=False)

    responses = {
        "anonymous": instance.client.get("/search", params={"q": "secret"}),
        "reader": instance.client.get("/search", params={"q": "secret"}, auth=instance.auth("reader")),
        "no query": instance.client.get("/search"),
        "no matches": instance.client.get("/search", params={"q": "absent"}),
    }
    for label, response in responses.items():
        assert response.status_code == 200, label
        assert response.headers["cache-control"] == "private, no-store", label


def test_reader_search_marks_private_names_uncacheable(instance) -> None:
    publish(instance, "secret", public=False)

    response = instance.client.get("/search", params={"q": "secret"}, auth=instance.auth("reader"))
    assert "secret" in response.text
    assert response.headers["cache-control"] == "private, no-store"
