"""The legacy XML-RPC search endpoint that ``pip search`` uses.

Requests are built with ``xmlrpc.client.dumps(..., methodname=...)`` and
responses parsed with ``xmlrpc.client.loads``: those are the two functions
``xmlrpc.client.ServerProxy`` itself uses, so this exercises the real wire
format rather than a hand-written approximation of it.
"""

from __future__ import annotations

import xmlrpc.client
from typing import Any

import pytest

from conftest import Instance, build_sdist, build_wheel, sdist_filename, wheel_filename
from pypiswasta import rpc
from pypiswasta.sdists import inspect_sdist
from pypiswasta.wheels import inspect_wheel

XML_MEDIA = "text/xml"


def call(
    client,
    params: Any,
    *,
    method: str = "search",
    auth: tuple[str, str] | None = None,
    path: str = "/pypi",
):
    """Post a methodCall exactly as ``pip search`` does.

    ``xmlrpc.client.dumps`` wants the arguments as a tuple, which is what
    ``ServerProxy`` passes it.
    """
    body = xmlrpc.client.dumps(tuple(params), methodname=method, allow_none=False)
    return client.post(path, content=body, headers={"Content-Type": XML_MEDIA}, auth=auth)


def hits(response) -> list[dict[str, str]]:
    """Parse the methodResponse exactly as ``ServerProxy`` does."""
    params, method = xmlrpc.client.loads(response.text)
    assert method is None, "a response document must not carry a method name"
    assert isinstance(params, tuple) and len(params) == 1, params
    result = params[0]
    assert isinstance(result, list)
    return result


def fault(response) -> xmlrpc.client.Fault:
    with pytest.raises(xmlrpc.client.Fault) as excinfo:
        xmlrpc.client.loads(response.text)
    return excinfo.value


def publish(
    instance: Instance,
    name: str,
    version: str = "1.0.0",
    *,
    summary: str | None = None,
    public: bool = True,
) -> None:
    wheel = build_wheel(name, version, summary=summary)
    response = instance.upload(wheel, wheel_filename(name, version))
    assert response.status_code == 200, response.text
    if public:
        promoted = instance.client.patch(
            f"/api/admin/packages/{name}",
            json={"visibility": "public"},
            auth=instance.auth("admin"),
        )
        assert promoted.status_code == 200


def pypi_search(instance: Instance, query: str, *, operator: str = "or", auth=None):
    """What pip sends: the same words in both the name and summary keys."""
    return call(instance.client, [{"name": [query], "summary": [query]}, operator], auth=auth)


# --------------------------------------------------------------------------- #
# the shape pip depends on
# --------------------------------------------------------------------------- #


def test_returns_the_three_fields_pip_reads(instance) -> None:
    publish(instance, "demo", "1.0.0", summary="A tiny demo package")

    response = pypi_search(instance, "demo")
    assert response.status_code == 200
    assert hits(response) == [{"name": "demo", "version": "1.0.0", "summary": "A tiny demo package"}]


def test_response_is_xml_and_uncached(instance) -> None:
    publish(instance, "demo", public=True)

    response = pypi_search(instance, "demo")
    assert response.headers["content-type"].startswith(XML_MEDIA)
    assert response.headers["cache-control"] == "no-store"


def test_empty_summary_is_an_empty_string(instance) -> None:
    """pip prints the summary, so the key must exist even when empty."""
    publish(instance, "demo")

    assert hits(pypi_search(instance, "demo"))[0]["summary"] == ""


def test_no_matches_returns_an_empty_list(instance) -> None:
    publish(instance, "demo", public=True)

    assert hits(pypi_search(instance, "absent")) == []


def test_search_before_setup_is_unavailable(client) -> None:
    """The setup gate applies here too; pip sees a plain HTTP error."""
    response = pypi_search(Instance(client=client, credentials={}), "demo")
    assert response.status_code == 503


def test_get_is_not_allowed(instance) -> None:
    assert instance.client.get("/pypi").status_code == 405


@pytest.mark.parametrize("path", ["/pypi", "/pypi/"])
def test_both_path_spellings_work(instance, path: str) -> None:
    """pip uses the --index URL verbatim, trailing slash and all."""
    publish(instance, "demo", public=True)

    response = call(instance.client, [{"name": ["demo"], "summary": ["demo"]}, "or"], path=path)
    assert response.status_code == 200
    assert hits(response)[0]["name"] == "demo"


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #


def test_matches_the_project_name(instance) -> None:
    publish(instance, "alpha-tools", public=True)
    publish(instance, "beta-tools", public=True)

    found = {hit["name"] for hit in hits(pypi_search(instance, "tools"))}
    assert found == {"alpha-tools", "beta-tools"}


def test_matches_a_substring_of_the_name(instance) -> None:
    publish(instance, "demo", public=True)

    assert hits(pypi_search(instance, "em"))[0]["name"] == "demo"


def test_matches_the_summary(instance) -> None:
    """pip's search is a description search as much as a name search."""
    publish(instance, "demo", summary="A tiny HTTP client")
    publish(instance, "other", summary="Unrelated")

    assert [hit["name"] for hit in hits(pypi_search(instance, "client"))] == ["demo"]


def test_name_matching_ignores_separators(instance) -> None:
    publish(instance, "My-Package", public=True)

    for term in ("My-Package", "my_package", "MY.PACKAGE", "my-package"):
        assert [hit["name"] for hit in hits(pypi_search(instance, term))] == ["my-package"], term


def test_summary_matching_is_case_insensitive(instance) -> None:
    publish(instance, "demo", summary="Widget Library")

    assert [hit["name"] for hit in hits(pypi_search(instance, "WIDGET"))] == ["demo"]


def test_reports_every_version(instance) -> None:
    publish(instance, "demo", "1.0.0", summary="First")
    publish(instance, "demo", "2.0.0", summary="Second")

    found = hits(pypi_search(instance, "demo"))
    assert sorted(hit["version"] for hit in found) == ["1.0.0", "2.0.0"]


def test_wheel_and_sdist_of_one_version_yield_one_hit(instance) -> None:
    publish(instance, "demo", "1.0.0")
    sdist = build_sdist("demo", "1.0.0")
    assert instance.upload(sdist, sdist_filename("demo", "1.0.0")).status_code == 200

    found = hits(pypi_search(instance, "demo"))
    assert len(found) == 1
    assert found[0]["version"] == "1.0.0"


def test_a_version_found_only_by_summary_reports_that_version(instance) -> None:
    publish(instance, "demo", "1.0.0", summary="Old and dull")
    publish(instance, "demo", "2.0.0", summary="Now with widgets")

    found = hits(pypi_search(instance, "widgets"))
    assert [(hit["name"], hit["version"]) for hit in found] == [("demo", "2.0.0")]


# --------------------------------------------------------------------------- #
# operators
# --------------------------------------------------------------------------- #


def test_or_matches_any_term(instance) -> None:
    publish(instance, "alpha", summary="widgets")
    publish(instance, "beta", summary="gadgets")

    found = {hit["name"] for hit in hits(pypi_search(instance, "widgets"))}
    assert found == {"alpha"}

    found = {hit["name"] for hit in hits(call(instance.client, [{"summary": ["widgets", "gadgets"]}, "or"]))}
    assert found == {"alpha", "beta"}


def test_and_requires_every_term(instance) -> None:
    publish(instance, "alpha", summary="widgets and gadgets")
    publish(instance, "beta", summary="widgets only")

    body = [{"name": ["widgets", "gadgets"], "summary": ["widgets", "gadgets"]}, "and"]
    found = {hit["name"] for hit in hits(call(instance.client, body))}
    assert found == {"alpha"}


def test_operator_is_case_insensitive(instance) -> None:
    publish(instance, "demo", public=True)

    body = [{"name": ["demo"], "summary": ["demo"]}, "OR"]
    assert hits(call(instance.client, body))[0]["name"] == "demo"


def test_terms_are_collapsed_across_the_two_keys(instance) -> None:
    """pip sends the same words in both keys; that must not double-count."""
    publish(instance, "demo", public=True)

    body = [{"name": ["demo"], "summary": ["demo"]}, "and"]
    assert [hit["name"] for hit in hits(call(instance.client, body))] == ["demo"]


# --------------------------------------------------------------------------- #
# visibility
# --------------------------------------------------------------------------- #


def test_private_projects_are_hidden_anonymously(instance) -> None:
    publish(instance, "secret", summary="A private thing", public=False)

    assert hits(pypi_search(instance, "secret")) == []
    assert hits(pypi_search(instance, "private")) == []


def test_reader_credentials_reveal_private_projects(instance) -> None:
    publish(instance, "secret", summary="A private thing", public=False)

    found = hits(pypi_search(instance, "secret", auth=instance.auth("reader")))
    assert [hit["name"] for hit in found] == ["secret"]


def test_upload_role_cannot_use_the_reader_view(instance) -> None:
    publish(instance, "secret", public=False)

    response = pypi_search(instance, "secret", auth=instance.auth("upload"))
    assert response.status_code == 403


def test_wrong_credentials_are_rejected(instance) -> None:
    assert pypi_search(instance, "demo", auth=("reader", "wrong")).status_code == 401


# --------------------------------------------------------------------------- #
# faults
# --------------------------------------------------------------------------- #


def test_unknown_method_faults(instance) -> None:
    response = call(instance.client, [], method="list_packages")
    assert response.status_code == 200
    assert fault(response).faultCode == rpc.METHOD_NOT_FOUND


@pytest.mark.parametrize(
    "params",
    [
        [],
        [{"name": ["demo"], "summary": ["demo"]}],
        ["not a struct", "or"],
        [{"name": ["demo"]}, "xor"],
        [{"name": [], "summary": []}, "or"],
    ],
)
def test_invalid_params_fault(instance, params: Any) -> None:
    response = call(instance.client, params)
    assert response.status_code == 200
    assert fault(response).faultCode == rpc.INVALID_PARAMS


def test_non_string_terms_fault(instance) -> None:
    response = call(instance.client, [{"name": [42], "summary": []}, "or"])
    assert fault(response).faultCode == rpc.INVALID_PARAMS


def test_malformed_body_faults(instance) -> None:
    response = instance.client.post(
        "/pypi",
        content=b"this is not xml",
        headers={"Content-Type": XML_MEDIA},
    )
    assert response.status_code == 200
    assert fault(response).faultCode == rpc.PARSE_ERROR


def test_method_response_instead_of_call_faults(instance) -> None:
    """A document that is not a methodCall must not be mistaken for one."""
    body = xmlrpc.client.dumps((1,), methodresponse=True, allow_none=False)
    response = instance.client.post("/pypi", content=body, headers={"Content-Type": XML_MEDIA})
    assert fault(response).faultCode == rpc.PARSE_ERROR


def test_oversized_body_faults(instance) -> None:
    body = b"<methodCall>" + b"x" * (rpc.MAX_REQUEST_BYTES + 1024)
    response = instance.client.post("/pypi", content=body, headers={"Content-Type": XML_MEDIA})
    assert response.status_code == 200
    assert fault(response).faultCode == rpc.PARSE_ERROR


# --------------------------------------------------------------------------- #
# summary extraction
# --------------------------------------------------------------------------- #


def test_wheel_summary_is_extracted(tmp_path) -> None:
    path = tmp_path / "demo-1.0.0-py3-none-any.whl"
    path.write_bytes(build_wheel("demo", "1.0.0", summary="A tiny demo package"))

    info = inspect_wheel(path, "demo-1.0.0-py3-none-any.whl")
    assert info.summary == "A tiny demo package"


def test_sdist_summary_is_extracted(tmp_path) -> None:
    path = tmp_path / "demo-1.0.0.tar.gz"
    path.write_bytes(build_sdist("demo", "1.0.0", summary="A tiny demo package"))

    info = inspect_sdist(path, "demo-1.0.0.tar.gz")
    assert info.summary == "A tiny demo package"


def test_missing_summary_becomes_empty(tmp_path) -> None:
    path = tmp_path / "demo-1.0.0-py3-none-any.whl"
    path.write_bytes(build_wheel("demo", "1.0.0", summary=None))

    assert inspect_wheel(path, "demo-1.0.0-py3-none-any.whl").summary == ""
