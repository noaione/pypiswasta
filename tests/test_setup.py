from __future__ import annotations

from conftest import ADMIN_USERNAME, parse_credentials


def test_setup_gate_redirects_get_requests(client) -> None:
    for path in ("/", "/simple/", "/simple/demo/", "/api/admin/packages"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/setup"


def test_setup_gate_blocks_writes(client) -> None:
    response = client.post("/legacy/", follow_redirects=False)
    assert response.status_code == 503


def test_healthz_is_reachable_before_setup(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_setup_page_is_served_before_setup(client) -> None:
    response = client.get("/setup")
    assert response.status_code == 200
    assert "First-time setup" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_setup_page_asks_only_for_a_username(client) -> None:
    response = client.get("/setup")
    assert 'name="username"' in response.text
    assert 'type="password"' not in response.text
    assert 'name="password"' not in response.text
    assert "generated automatically" in response.text


def test_setup_generates_every_password(client) -> None:
    response = client.post("/setup", data={"username": ADMIN_USERNAME})
    assert response.status_code == 200

    credentials = parse_credentials(response.text)
    assert set(credentials) == {"admin", "upload", "reader"}
    assert credentials["admin"][0] == ADMIN_USERNAME

    passwords = [value[1] for value in credentials.values()]
    assert len(set(passwords)) == 3
    assert all(len(password) >= 42 for password in passwords)

    # The gate is now open and the generated admin credential works.
    assert client.get("/").status_code == 200
    assert client.get("/api/admin/packages", auth=credentials["admin"]).status_code == 200


def test_setup_ignores_submitted_password_fields(client) -> None:
    """The page has no password inputs, and any posted ones are ignored."""
    response = client.post(
        "/setup",
        data={
            "username": ADMIN_USERNAME,
            "password": "a-chosen-password",
            "password_confirm": "a-chosen-password",
        },
    )
    assert response.status_code == 200

    credentials = parse_credentials(response.text)
    assert credentials["admin"][1] != "a-chosen-password"
    assert len(credentials["admin"][1]) >= 42
    assert client.get("/api/admin/packages", auth=(ADMIN_USERNAME, "a-chosen-password")).status_code == 401


def test_setup_cannot_be_repeated(client) -> None:
    assert client.post("/setup", data={"username": ADMIN_USERNAME}).status_code == 200

    response = client.get("/setup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    # A second submission is redirected rather than replacing credentials.
    again = client.post("/setup", data={"username": "intruder"}, follow_redirects=False)
    assert again.status_code == 303
    assert client.get("/api/admin/packages", auth=("intruder", "whatever")).status_code == 401


def test_setup_rejects_invalid_username(client) -> None:
    response = client.post("/setup", data={"username": "bad user:name"})
    assert response.status_code == 200
    assert "Username must be" in response.text
    assert 'value="bad user:name"' in response.text

    # Still uninitialized, so the gate keeps holding.
    assert client.get("/", follow_redirects=False).status_code == 303


def test_setup_survives_username_collision_with_generated_roles(client) -> None:
    response = client.post("/setup", data={"username": "reader"})
    assert response.status_code == 200

    credentials = parse_credentials(response.text)
    assert credentials["admin"][0] == "reader"
    assert credentials["reader"][0] != "reader"
    assert len({value[0] for value in credentials.values()}) == 3


def test_landing_footer_stays_inside_the_content_container(client) -> None:
    """The footer must live inside <main> so it shares the centred max-width."""
    assert client.post("/setup", data={"username": ADMIN_USERNAME}).status_code == 200

    page = client.get("/").text
    assert page.count("<footer>") == 1

    inside_main = page.split("<main>", 1)[1].split("</main>", 1)[0]
    assert "<footer>" in inside_main

    # Anything rendered after </main> escapes the max-width container.
    after_main = page.split("</main>", 1)[1]
    assert "<footer>" not in after_main
    assert "<a " not in after_main
