import asyncio
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import jwt

from .config import APP_NAME, AUTH_ISSUER, AUTHORIZE_URL, DISCOVERY_URL, RESOURCE, SCOPES, TOKEN_URL
from .errors import GatewayError
from .storage import Store, public_account


@dataclass
class PendingLogin:
    verifier: str
    nonce: str
    callback: str
    expires_at: float
    account: dict[str, Any] | None
    client_id: str


class Auth:
    def __init__(self, store: Store, client: httpx.AsyncClient, callback: str):
        self.store = store
        self.client = client
        self.callback_uri = callback
        self.pending: dict[str, PendingLogin] = {}
        self._refresh_lock = asyncio.Lock()
        self._discovery: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._jwks_at = 0.0
        self._retry_client_id: str | None = None

    def start(self, account_id: str | None = None, *, new: bool = False) -> str:
        if new or account_id is not None:
            self._retry_client_id = None
        retry_client_id = self._retry_client_id if not new and account_id is None else None
        account = (
            None
            if new or retry_client_id
            else (self.store.get(account_id) if account_id else self.store.active())
        )
        if account_id and account is None and not new:
            raise GatewayError(404, "No saved account has that id.", "account_not_found")
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        now = time.time()
        self.pending = {key: item for key, item in self.pending.items() if item.expires_at > now}
        if len(self.pending) >= 10:
            raise GatewayError(429, "Too many pending sign-ins. Try again shortly.", "login_busy")
        client_id = retry_client_id or (account["client_id"] if account else "dynamic_agent_client")
        self.pending[state] = PendingLogin(
            verifier, nonce, self.callback_uri, now + 600, account, client_id
        )
        parameters = {
            "client_id": client_id,
            "ext_agent_host_id": self.store.host_id,
            "response_type": "code",
            "redirect_uri": self.callback_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("="),
        }
        if account:
            if account.get("id_token"):
                parameters["id_token_hint"] = account["id_token"]
            if account.get("email"):
                parameters["login_hint"] = account["email"]
            if "chatgpt.tokens.use.direct" not in account.get("scopes", []):
                parameters["prompt"] = "consent"
        elif client_id == "dynamic_agent_client":
            parameters["agent_name_hint"] = APP_NAME
        return f"{AUTHORIZE_URL}?{urlencode(parameters)}"

    async def discovery(self) -> dict[str, Any]:
        if self._discovery is None:
            response = await self.client.get(DISCOVERY_URL, timeout=30)
            if response.status_code != 200:
                raise GatewayError(
                    502, "OpenAI sign-in discovery is unavailable.", "oauth_unavailable"
                )
            metadata = response.json()
            if metadata.get("issuer") != AUTH_ISSUER:
                raise GatewayError(
                    502, "The OpenAI identity issuer did not match.", "invalid_issuer"
                )
            for key in ("jwks_uri", "revocation_endpoint"):
                if key not in metadata:
                    continue
                address = urlparse(metadata[key])
                if address.scheme != "https" or address.netloc != "auth.openai.com":
                    raise GatewayError(502, "Unexpected identity endpoint.", "invalid_issuer")
            if not metadata.get("jwks_uri"):
                raise GatewayError(
                    502, "OpenAI did not publish a signing-key URL.", "oauth_unavailable"
                )
            self._discovery = metadata
        return self._discovery

    async def identity(
        self, token: str, client_id: str, *, nonce: str | None = None
    ) -> dict[str, Any]:
        metadata = await self.discovery()
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in {"RS256", "ES256"} or not header.get("kid"):
                raise jwt.InvalidTokenError("Unexpected signature")
            if (
                self._jwks is None
                or time.time() - self._jwks_at > 300
                or not any(key.get("kid") == header["kid"] for key in self._jwks.get("keys", []))
            ):
                response = await self.client.get(metadata["jwks_uri"], timeout=30)
                if response.status_code != 200:
                    raise GatewayError(
                        502, "OpenAI signing keys are unavailable.", "oauth_unavailable"
                    )
                self._jwks = response.json()
                self._jwks_at = time.time()
            keys = [key for key in self._jwks.get("keys", []) if key.get("kid") == header["kid"]]
            if len(keys) != 1:
                raise jwt.InvalidTokenError("Unknown signing key")
            key = jwt.PyJWK.from_dict(keys[0])
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[header["alg"]],
                issuer=AUTH_ISSUER,
                audience=client_id,
                leeway=5,
                options={"require": ["sub", "exp", "iat"]},
            )
            if not isinstance(claims.get("sub"), str) or not claims["sub"]:
                raise jwt.InvalidTokenError("Missing subject")
            if nonce is not None and not secrets.compare_digest(
                str(claims.get("nonce", "")), nonce
            ):
                raise jwt.InvalidTokenError("Nonce mismatch")
            return claims
        except (jwt.PyJWTError, ValueError, KeyError, TypeError) as error:
            raise GatewayError(
                400, "OpenAI's signed identity could not be verified.", "invalid_identity"
            ) from error

    async def exchange(self, form: dict[str, str]) -> dict[str, Any]:
        response = await self.client.post(TOKEN_URL, data=form, timeout=30)
        if response.status_code != 200:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            code = payload.get("error", "oauth_error")
            if isinstance(code, dict):
                code = code.get("code", "oauth_error")
            raise GatewayError(
                response.status_code,
                "OpenAI could not renew or complete this sign-in. "
                "Sign in again if the grant expired.",
                str(code),
                request_id=response.headers.get("x-request-id"),
            )
        tokens = response.json()
        if (
            not isinstance(tokens.get("access_token"), str)
            or not tokens["access_token"]
            or str(tokens.get("token_type", "")).lower() != "bearer"
            or not isinstance(tokens.get("expires_in"), (int, float))
            or tokens["expires_in"] <= 0
        ):
            raise GatewayError(
                502, "OpenAI returned an incomplete token response.", "invalid_token_response"
            )
        return tokens

    async def callback(self, query: dict[str, str]) -> dict[str, Any]:
        # One-time consumption also prevents a failed code from being tried a second time.
        pending = self.pending.pop(query.get("state", ""), None)
        if pending is None or pending.expires_at < time.time():
            raise GatewayError(
                400, "This sign-in expired or does not match. Start again.", "invalid_state"
            )
        if query.get("error"):
            raise GatewayError(400, "ChatGPT sign-in was not completed.", "access_denied")
        issued = query.get("client_id")
        if pending.client_id != "dynamic_agent_client":
            client_id = pending.client_id
            if issued is not None and issued != client_id:
                raise GatewayError(
                    400, "The returning account's client id changed.", "client_mismatch"
                )
        else:
            if not issued or issued == "dynamic_agent_client":
                raise GatewayError(
                    400, "OpenAI did not issue a client id.", "registration_incomplete"
                )
            client_id = issued
        if not query.get("code"):
            raise GatewayError(400, "OpenAI did not return an authorization code.", "missing_code")
        try:
            tokens = await self.exchange(
                {
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": query["code"],
                    "code_verifier": pending.verifier,
                    "redirect_uri": pending.callback,
                    "resource": RESOURCE,
                }
            )
        except GatewayError as error:
            if error.code == "invalid_grant" and pending.account is None:
                # Keep a failed registration separate from all verified accounts.
                # A fresh authorization may reuse this issued ID, never the consumed code.
                self._retry_client_id = client_id
            raise
        if not isinstance(tokens.get("id_token"), str):
            raise GatewayError(
                502, "OpenAI did not return an identity token.", "invalid_token_response"
            )
        claims = await self.identity(tokens["id_token"], client_id, nonce=pending.nonce)
        if pending.account and claims["sub"] != pending.account["subject"]:
            raise GatewayError(
                400,
                "The signed-in account does not match the selected account.",
                "account_mismatch",
            )
        account = {
            "issuer": AUTH_ISSUER,
            "subject": claims["sub"],
            "email": claims.get("email"),
            "client_id": client_id,
            "host_id": self.store.host_id,
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token"),
            "id_token": tokens["id_token"],
            "scopes": str(tokens.get("scope", "")).split(),
            "expires_at": time.time() + tokens["expires_in"],
            "earliest_refresh_at": tokens.get("earliest_refresh_at", 0),
        }
        saved = self.store.save(account, active=True)
        if self._retry_client_id == client_id:
            self._retry_client_id = None
        return public_account(saved)

    async def credentials(self, *, rejected: dict[str, Any] | None = None) -> dict[str, Any]:
        # The server holds an exclusive process lock; this lock serializes all token rotation.
        async with self._refresh_lock:
            account = self.store.active()
            if account is None or not account.get("access_token"):
                raise GatewayError(
                    401, "Connect a ChatGPT account using chatgpt-gateway login.", "not_signed_in"
                )
            if rejected is not None and account["id"] != rejected["id"]:
                raise GatewayError(
                    409, "The selected account changed; retry explicitly.", "account_changed"
                )
            if "chatgpt.tokens.use.direct" not in account.get("scopes", []):
                raise GatewayError(
                    403,
                    "This account has not allowed ChatGPT plan usage. Sign in again to enable it.",
                    "plan_permission_missing",
                )
            # A rejection may race another request's renewal. Reuse that replacement;
            # never rotate it again or move this request to another account.
            if rejected is not None and account["access_token"] != rejected["access_token"]:
                return account
            earliest = account.get("earliest_refresh_at") or 0
            too_early = isinstance(earliest, (int, float)) and earliest > time.time()
            if account.get("expires_at", 0) > time.time() + 60 and (rejected is None or too_early):
                return account
            if not account.get("refresh_token"):
                raise GatewayError(
                    401, "The sign-in expired. Connect this account again.", "sign_in_expired"
                )
            if too_early:
                raise GatewayError(
                    503,
                    "Token renewal is not yet available. Try again shortly.",
                    "refresh_not_ready",
                )
            try:
                tokens = await self.exchange(
                    {
                        "grant_type": "refresh_token",
                        "client_id": account["client_id"],
                        "refresh_token": account["refresh_token"],
                        "resource": RESOURCE,
                    }
                )
            except GatewayError as error:
                if error.code in {
                    "invalid_grant",
                    "invalid_refresh_token",
                    "token_expired",
                    "refresh_token_expired",
                    "refresh_token_invalidated",
                    "refresh_token_reused",
                }:
                    self.store.clear_tokens(account["id"])
                    raise GatewayError(
                        401, "This ChatGPT session ended. Sign in again.", "sign_in_expired"
                    ) from error
                raise
            if not isinstance(tokens.get("refresh_token"), str) or not tokens["refresh_token"]:
                raise GatewayError(
                    502, "OpenAI did not rotate the refresh token.", "invalid_token_response"
                )
            if tokens.get("id_token"):
                claims = await self.identity(tokens["id_token"], account["client_id"])
                if claims["sub"] != account["subject"]:
                    raise GatewayError(
                        401, "Token renewal changed account identity.", "account_mismatch"
                    )
            updated = {
                **account,
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "id_token": tokens.get("id_token") or account.get("id_token"),
                "expires_at": time.time() + tokens["expires_in"],
                "scopes": str(tokens["scope"]).split() if "scope" in tokens else account["scopes"],
                "earliest_refresh_at": tokens.get("earliest_refresh_at", 0),
            }
            updated = self.store.save(updated)
            if "chatgpt.tokens.use.direct" not in updated["scopes"]:
                raise GatewayError(
                    403, "ChatGPT plan permission is no longer granted.", "plan_permission_missing"
                )
            return updated

    async def logout(self) -> bool:
        async with self._refresh_lock:
            account = self.store.active()
            confirmed = not account or not account.get("refresh_token")
            if account and account.get("refresh_token"):
                try:
                    metadata = await self.discovery()
                    endpoint = metadata.get("revocation_endpoint")
                    if endpoint:
                        response = await self.client.post(
                            endpoint,
                            data={
                                "token": account["refresh_token"],
                                "token_type_hint": "refresh_token",
                                "client_id": account["client_id"],
                            },
                            timeout=30,
                        )
                        confirmed = response.status_code == 200
                except (httpx.HTTPError, GatewayError):
                    confirmed = False
            if account:
                self.store.clear_tokens(account["id"])
            return confirmed
