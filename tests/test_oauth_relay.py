from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from oauth_relay import PROTOCOL
from oauth_relay.relay import LOOPBACK, RelayConfig, create_relay

HOST = "relay.test"
PORTAL = "synthetic.bitrix24.ru"
PREFIX = "/integrations/meeting-archive"
VALID = {"code": "synthetic-code", "state": "S" * 43, "domain": PORTAL,
    "member_id": "synthetic-member", "scope": "call,user_basic,disk", "server_domain": "oauth.bitrix.info"}


@pytest.fixture
def relay_client():
    app = create_relay(RelayConfig(HOST, frozenset({PORTAL}), PREFIX))
    return TestClient(app, base_url="https://" + HOST, follow_redirects=False)


def test_relay_health_contract_and_base_path(relay_client):
    response = relay_client.get(PREFIX + "/oauth/health")
    assert response.status_code == 200
    assert response.json() == {"protocol": PROTOCOL, "version": 1,
        "callback_path": PREFIX + "/oauth/callback", "install_path": PREFIX + "/oauth/install",
        "loopback": LOOPBACK, "credentials": "desktop-only", "allowed_portals": [PORTAL]}
    assert relay_client.get("/oauth/health").status_code == 404
    assert relay_client.get(PREFIX + "/oauth/health/").status_code == 404
    assert relay_client.get(PREFIX + "/oauth/callback/", params=VALID).status_code == 404


def test_relay_install_info_has_no_browser_finish(relay_client):
    response = relay_client.get(PREFIX + "/oauth/install")
    assert response.status_code == 200
    assert response.json()["self_finish"] is False
    assert response.json()["token_storage"] == "none"
    assert response.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("event", ["ONAPPINSTALL", "ONAPPUSERREADY"])
@pytest.mark.parametrize("kind", ["json", "form"])
def test_relay_install_ack_discards_tokens(relay_client, event, kind):
    if kind == "json":
        response = relay_client.post(PREFIX + "/oauth/install", json={"event": event, "auth": {
            "domain": PORTAL, "access_token": "synthetic-secret", "refresh_token": "synthetic-refresh",
            "application_token": "synthetic-app"}})
    else:
        response = relay_client.post(PREFIX + "/oauth/install", data={"event": event,
            "auth[domain]": PORTAL, "auth[access_token]": "synthetic-secret"})
    assert response.status_code == 200 and response.content == b""
    assert "synthetic-secret" not in str(response.headers)


@pytest.mark.parametrize("payload", [None, [], {"event": "ONAPPINSTALL", "auth": []},
    {"event": "ONAPPINSTALL", "auth": {"domain": "foreign.bitrix24.ru"}},
    {"event": "ONAPPUNINSTALL", "auth": {"domain": PORTAL}}])
def test_relay_install_rejects_bad_event_body_and_unlisted_portal(relay_client, payload):
    assert relay_client.post(PREFIX + "/oauth/install", json=payload).status_code in {400, 415}


@pytest.mark.parametrize("body,content_type", [
    ('{"event":"ONAPPINSTALL","event":"ONAPPINSTALL","auth":{"domain":"' + PORTAL + '"}}', "application/json"),
    ('{"event":"ONAPPINSTALL","auth":{"domain":"' + PORTAL + '","domain":"' + PORTAL + '"}}', "application/json"),
    ("event=ONAPPINSTALL&event=ONAPPINSTALL&auth%5Bdomain%5D=" + PORTAL, "application/x-www-form-urlencoded"),
    ("invalid-form-entry", "application/x-www-form-urlencoded"), ("not-json", "application/json"),
])
def test_relay_install_rejects_duplicates_or_parse_error_without_echo(relay_client, body, content_type):
    response = relay_client.post(PREFIX + "/oauth/install", content=body,
        headers={"content-type": content_type})
    assert response.status_code == 400 and response.content == b""


def test_relay_install_rejects_body_limit_and_query(relay_client):
    assert relay_client.post(PREFIX + "/oauth/install", content=b"x" * 65537).status_code == 413
    assert relay_client.post(PREFIX + "/oauth/install?access_token=synthetic", json={}).status_code == 400
    assert relay_client.get(PREFIX + "/oauth/install?access_token=synthetic").status_code == 400
    assert relay_client.get(PREFIX + "/oauth/health?access_token=synthetic").status_code == 400


def test_relay_callback_fixed_loopback_allowlist_headers(relay_client):
    response = relay_client.get(PREFIX + "/oauth/callback", params=VALID)
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.scheme + "://" + location.netloc + location.path == LOOPBACK
    assert parse_qs(location.query) == {key: [value] for key, value in VALID.items() if key != "server_domain"}
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("additional", [{"access_token": "synthetic"}, {"refresh_token": "synthetic"},
    {"client_secret": "synthetic"}, {"callback_url": "https://malicious.invalid"},
    {"target": "https://malicious.invalid"}])
def test_relay_callback_rejects_tokens_and_open_redirect_inputs(relay_client, additional):
    response = relay_client.get(PREFIX + "/oauth/callback", params={**VALID, **additional})
    assert response.status_code == 400 and response.content == b""
    assert "location" not in response.headers


@pytest.mark.parametrize("replacement", [{"state": "short"}, {"state": "x" * 257},
    {"domain": "foreign.bitrix24.ru"}, {"server_domain": "foreign.invalid"},
    {"code": "x" * 257}, {"code": "x\r\nLocation:bad"}, {"member_id": "x" * 129}, {"scope": "disk,user_basic"}])
def test_relay_callback_rejects_wrong_or_unbounded_fields(relay_client, replacement):
    assert relay_client.get(PREFIX + "/oauth/callback", params={**VALID, **replacement}).status_code == 400


@pytest.mark.parametrize("field", ["code", "state", "domain", "member_id", "scope"])
def test_relay_callback_requires_fields(relay_client, field):
    assert relay_client.get(PREFIX + "/oauth/callback",
        params={key: value for key, value in VALID.items() if key != field}).status_code == 400


def test_relay_callback_rejects_duplicate_fields(relay_client):
    assert relay_client.get(PREFIX + "/oauth/callback", params=list(VALID.items()) + [("state", "T" * 43)]).status_code == 400


def test_relay_rejects_host_spoof_and_limits_query(relay_client):
    response = relay_client.get(PREFIX + "/oauth/callback", params=VALID, headers={"host": "foreign.invalid"})
    assert response.status_code == 403 and response.headers["referrer-policy"] == "no-referrer"
    assert relay_client.get(PREFIX + "/oauth/health", headers={"host": HOST + ":443"}).status_code == 200
    assert relay_client.get(PREFIX + "/oauth/callback?" + "x" * 4097).status_code == 414


def test_relay_env_configuration_fail_closed():
    with pytest.raises(ValueError, match="PUBLIC_HOST"):
        RelayConfig.from_env({})
    with pytest.raises(ValueError, match="ALLOWED_PORTALS"):
        RelayConfig.from_env({"PUBLIC_HOST": HOST})
    config = RelayConfig.from_env({"PUBLIC_HOST": HOST, "ALLOWED_PORTALS": PORTAL + ",foreign.bitrix24.ru",
        "RELAY_BASE_PATH": PREFIX})
    assert config.allowed_portals == frozenset({PORTAL, "foreign.bitrix24.ru"})


@pytest.mark.parametrize("host", ["http://relay.test", "relay.test:80", "relay.test/path", "127.0.0.1",
    "user@relay.test", "Relay.Test", "relay.test?key=value", "-relay.test"])
def test_relay_rejects_invalid_public_host(host):
    with pytest.raises(ValueError, match="PUBLIC_HOST"):
        RelayConfig(host, frozenset({PORTAL}))


@pytest.mark.parametrize("path", ["/", "oauth", "/oauth/", "/..", "/oauth%2Fcallback", "/oauth//callback",
    "/oauth?x=y", "/oauth#fragment"])
def test_relay_rejects_invalid_prefix(path):
    with pytest.raises(ValueError, match="RELAY_BASE_PATH"):
        RelayConfig(HOST, frozenset({PORTAL}), path)
