"""
Agent-identity middleware (ported from the Kaplan MCP servers): every request
must carry a Google-signed OIDC ID token minted for the agent's service
account, with an audience equal to one of this server's URLs. The caller's
identity is cryptographically provable and individually revocable — never a
shared secret.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("mcp_source_server.auth")

EXPECTED_AUDIENCE = [a.strip() for a in os.getenv("MCP_EXPECTED_AUDIENCE", "").split(",") if a.strip()]
EXPECTED_CALLER = os.getenv("MCP_EXPECTED_CALLER_EMAIL", "")


def verify(token: str) -> bool:
    if not EXPECTED_CALLER or not EXPECTED_AUDIENCE:
        log.error("MCP_EXPECTED_CALLER_EMAIL / MCP_EXPECTED_AUDIENCE not configured — refusing all callers")
        return False
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
        claims = id_token.verify_oauth2_token(token, google_requests.Request(), audience=EXPECTED_AUDIENCE)
    except Exception as e:
        log.warning("token rejected: %s", e)
        return False
    if claims.get("email") != EXPECTED_CALLER or not claims.get("email_verified", False):
        log.warning("caller %s is not the expected agent identity", claims.get("email"))
        return False
    return True


class AgentIdentityMiddleware:
    """Pure ASGI middleware (no response buffering — streaming responses pass
    through untouched)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not verify(auth[7:]):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        return await self.app(scope, receive, send)
