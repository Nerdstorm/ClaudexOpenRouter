import asyncio
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from conftest import account
from cryptography.hazmat.primitives.asymmetric import rsa

from chatgpt_gateway.auth import Auth
from chatgpt_gateway.config import AUTH_ISSUER, RESOURCE
from chatgpt_gateway.errors import GatewayError
from chatgpt_gateway.storage import Store


@pytest.fixture
def keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public["kid"] = "test-key"
    return private, public


def signed(keys, nonce, client_id="oaiapp_issued", **changes):
    return jwt.encode(
        {
            "iss": AUTH_ISSUER,
            "aud": client_id,
            "sub": "test-subject",
            "email": "tester@example.test",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "nonce": nonce,
            **changes,
        },
        keys[0],
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


def oauth_backend(keys, tokens):
    def handler(request):
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": AUTH_ISSUER,
                    "jwks_uri": f"{AUTH_ISSUER}/.well-known/jwks.json",
                    "revocation_endpoint": f"{AUTH_ISSUER}/revoke",
                },
            )
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [keys[1]]})
        assert request.url.path == "/api/accounts/oauth/token"
        return httpx.Response(200, json=tokens)

    return handler


async def test_full_registration_verifies_identity_and_saves_private_account(tmp_path, keys):
    store = Store(tmp_path)
    tokens = {
        "access_token": "access-secret",
        "refresh_token": "refresh-secret",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid chatgpt.tokens.use.direct",
    }
    requests = []
    handler = oauth_backend(keys, tokens)

    def capture(request):
        requests.append(request)
        return handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
        auth = Auth(store, client, "http://127.0.0.1:8317/auth/callback")
        query = parse_qs(urlparse(auth.start(new=True)).query)
        assert query["client_id"] == ["dynamic_agent_client"]
        assert query["agent_name_hint"] == ["ChatGPT Plan Gateway"]
        assert query["ext_agent_host_id"] == [store.host_id]
        assert query["resource"] == [RESOURCE]
        tokens["id_token"] = signed(keys, query["nonce"][0])
        result = await auth.callback(
            {"state": query["state"][0], "client_id": "oaiapp_issued", "code": "test-code"}
        )
        assert result["plan_enabled"] is True
        assert "access_token" not in result and "id_token" not in result
        assert store.active()["client_id"] == "oaiapp_issued"
        exchange = next(r for r in requests if r.method == "POST")
        form = parse_qs(exchange.content.decode())
        assert form["client_id"] == ["oaiapp_issued"]
        assert form["redirect_uri"] == query["redirect_uri"]
        assert "code_verifier" in form
        assert (tmp_path / "accounts" / f"{result['id']}.json").stat().st_mode & 0o777 == 0o600
        with pytest.raises(GatewayError, match="expired"):
            await auth.callback({"state": query["state"][0], "code": "test-code"})


@pytest.mark.parametrize(
    "changes",
    [{"nonce": "wrong"}, {"aud": "wrong"}, {"iss": "https://wrong.test"}, {"exp": 1}, {"sub": ""}],
)
async def test_invalid_identity_never_replaces_active_account(tmp_path, keys, changes):
    store = Store(tmp_path)
    original = store.save(account(), active=True)
    tokens = {
        "access_token": "new-secret",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "chatgpt.tokens.use.direct",
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(oauth_backend(keys, tokens))
    ) as client:
        auth = Auth(store, client, "http://127.0.0.1:8317/auth/callback")
        query = parse_qs(urlparse(auth.start(new=True)).query)
        nonce = changes.get("nonce", query["nonce"][0])
        tokens["id_token"] = signed(
            keys, nonce, **{k: v for k, v in changes.items() if k != "nonce"}
        )
        with pytest.raises(GatewayError):
            await auth.callback(
                {"state": query["state"][0], "client_id": "oaiapp_issued", "code": "test-code"}
            )
        assert store.active()["id"] == original["id"]
        assert store.active()["access_token"] == "test-access-secret"


async def test_callback_without_state_or_issued_id_never_exchanges(tmp_path):
    store = Store(tmp_path)

    def no_network(request):
        raise AssertionError("No code exchange should occur")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_network)) as client:
        auth = Auth(store, client, "http://127.0.0.1:8317/auth/callback")
        with pytest.raises(GatewayError):
            await auth.callback({"code": "fake"})
        query = parse_qs(urlparse(auth.start(new=True)).query)
        with pytest.raises(GatewayError) as error:
            await auth.callback({"state": query["state"][0], "code": "fake"})
        assert error.value.code == "registration_incomplete"


async def test_returning_signin_reuses_identity_and_rejects_client_change(tmp_path):
    store = Store(tmp_path)
    saved = store.save(account(), active=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError()))
    ) as client:
        auth = Auth(store, client, "http://127.0.0.1:8317/auth/callback")
        query = parse_qs(urlparse(auth.start()).query)
        assert query["client_id"] == [saved["client_id"]]
        assert query["id_token_hint"] == [saved["id_token"]]
        assert "agent_name_hint" not in query
        with pytest.raises(GatewayError) as error:
            await auth.callback({"state": query["state"][0], "client_id": "another", "code": "x"})
        assert error.value.code == "client_mismatch"


async def test_refresh_rotates_once_for_concurrent_requests(tmp_path):
    store = Store(tmp_path)
    store.save(account(expires_at=0), active=True)
    refreshes = []

    async def handler(request):
        refreshes.append(parse_qs(request.content.decode()))
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = Auth(store, client, "http://127.0.0.1:8317/auth/callback")
        a, b = await asyncio.gather(auth.credentials(), auth.credentials())
        assert len(refreshes) == 1 and a["access_token"] == b["access_token"] == "new-access"
        assert refreshes[0]["client_id"] == ["oaiapp_test"]
        assert "scope" not in refreshes[0]
        assert store.active()["refresh_token"] == "new-refresh"


async def test_terminal_refresh_failure_clears_tokens_not_registration(tmp_path):
    store = Store(tmp_path)
    saved = store.save(account(expires_at=0), active=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(400, json={"error": "invalid_grant"})
        )
    ) as client:
        auth = Auth(store, client, "http://127.0.0.1:8317/auth/callback")
        with pytest.raises(GatewayError):
            await auth.credentials()
        assert store.active()["client_id"] == saved["client_id"]
        assert "refresh_token" not in store.active()


async def test_temporary_refresh_failure_preserves_credentials(tmp_path):
    store = Store(tmp_path)
    store.save(account(expires_at=0), active=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(503, json={"error": "temporarily_unavailable"})
        )
    ) as client:
        auth = Auth(store, client, "http://127.0.0.1:8317/auth/callback")
        with pytest.raises(GatewayError):
            await auth.credentials()
        assert store.active()["refresh_token"] == "test-refresh-secret"


async def test_logout_clears_local_tokens_even_when_revocation_unconfirmed(tmp_path, keys):
    store = Store(tmp_path)
    store.save(account(), active=True)
    base = oauth_backend(keys, {})

    def handler(request):
        return httpx.Response(503) if request.url.path == "/revoke" else base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = Auth(store, client, "http://127.0.0.1:8317/auth/callback")
        assert await auth.logout() is False
        assert "access_token" not in store.active()


async def test_expired_first_code_reauthorizes_issued_client_without_replacing_active(
    tmp_path, keys
):
    store = Store(tmp_path)
    original = store.save(account(), active=True)
    tokens = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "chatgpt.tokens.use.direct",
    }
    base = oauth_backend(keys, tokens)
    expired = True

    def handler(request):
        if expired and request.url.path == "/api/accounts/oauth/token":
            return httpx.Response(400, json={"error": "invalid_grant"})
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = Auth(store, client, "http://127.0.0.1:8317/auth/callback")
        first = parse_qs(urlparse(auth.start(new=True)).query)
        with pytest.raises(GatewayError):
            await auth.callback(
                {"state": first["state"][0], "client_id": "oaiapp_issued", "code": "expired"}
            )
        assert store.active()["id"] == original["id"]
        assert len(store.accounts()) == 1
        retry = parse_qs(urlparse(auth.start()).query)
        assert retry["client_id"] == ["oaiapp_issued"]
        assert retry["state"] != first["state"] and retry["nonce"] != first["nonce"]
        assert "agent_name_hint" not in retry and "id_token_hint" not in retry
        expired = False
        tokens["id_token"] = signed(keys, retry["nonce"][0])
        result = await auth.callback({"state": retry["state"][0], "code": "fresh"})
        assert result["id"] != original["id"] and result["plan_enabled"] is True
        assert store.get(original["id"])["access_token"] == "test-access-secret"
