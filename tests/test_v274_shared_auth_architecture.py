"""
Test Suite: FinPlus+ Single Master Token Authority & Peer Sharing Architecture
==============================================================================
Validates:
1. Only ONE app (Master) auto-generates tokens via TOTP.
2. Peer apps operate in 'client' mode and never independently call the broker /generate/token.
3. Master automatically broadcasts newly generated tokens to registered peer apps.
4. Master exposes GET /api/auth/token for on-demand pull by authorized peer services.
5. Client gracefully fetches from Master when token is expired/missing.
"""

import os
import sys
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
_APPS_ROOT = _ROOT.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_APPS_ROOT) not in sys.path:
    sys.path.insert(0, str(_APPS_ROOT))

from fastapi.testclient import TestClient

from finplus_core.auth.indmoney_client import (
    INDMoneyAuthClient,
    INDMoneyAuthError,
    RateLimitCooldownError,
)
from src.api.server import app


@pytest.fixture
def test_client():
    return TestClient(app)


def test_auth_client_mode_does_not_require_totp_keys(tmp_path):
    """Client mode must instantiate without requiring INDMONEY_TOTP_SECRET or INDMONEY_MPIN."""
    env_file = tmp_path / ".env"
    env_file.write_text("INDMONEY_CLIENT_ID=test_client_id\n", encoding="utf-8")

    client = INDMoneyAuthClient(
        client_id="test_client_id",
        totp_secret=None,
        mpin=None,
        env_file_path=env_file,
        auth_role="client",
    )
    assert client.auth_role == "client"


def test_client_mode_never_generates_totp_and_fetches_from_master(tmp_path, monkeypatch):
    """In client mode, token refresh delegates to Auth Master and NEVER calls _execute_token_request."""
    monkeypatch.delenv("INDMONEY_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("INDSTOCKS_TOKEN", raising=False)
    empty_token_file = tmp_path / "empty_token.json"
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    client = INDMoneyAuthClient(
        client_id="test_client_id",
        totp_secret=None,
        mpin=None,
        auth_role="client",
        master_url="http://127.0.0.1:8001",
        token_file_path=empty_token_file,
        env_file_path=empty_env,
    )
    client.access_token = None

    # Mock _fetch_token_from_master to simulate master response
    mock_token = "mock_jwt_token.eyJzdWIiOiIxMjM0NSIsImV4cCI6OTk5OTk5OTk5OSwiY2xpZW50SUQiOiI5R0dEUyJ9.signature"
    with patch.object(client, "_fetch_token_from_master", return_value=mock_token) as mock_fetch, \
         patch.object(client, "_execute_token_request") as mock_exec:
        
        tok = client.get_valid_token(auto_refresh=True)
        assert tok == mock_token
        assert mock_fetch.called
        assert not mock_exec.called, "Client mode must NEVER execute TOTP token generation!"


def test_master_mode_broadcasts_fresh_token_to_peers(tmp_path):
    """When Master generates a token, it must broadcast it to peer /api/admin/set-token endpoints."""
    peer_url = "https://finplus-index-breadth.onrender.com/api/admin/set-token"
    client = INDMoneyAuthClient(
        client_id="9GGDS",
        totp_secret="JBSWY3DPEHPK3PXP",
        mpin="123456",
        auth_role="master",
        peer_urls=[peer_url],
    )

    mock_new_tok = "fresh_jwt_token.eyJzdWIiOiIxMjM0NSIsImV4cCI6OTk5OTk5OTk5OSwiY2xpZW50SUQiOiI5R0dEUyJ9.sig"
    with patch.object(client, "_execute_token_request", return_value=mock_new_tok), \
         patch.object(client, "get_cooldown_remaining", return_value=0.0), \
         patch("requests.post") as mock_post:
        
        mock_post.return_value = MagicMock(status_code=200)
        tok = client.generate_fresh_token(persist_to_env=False)
        assert tok == mock_new_tok

        # Verify broadcast occurred to peer
        assert mock_post.called
        call_args, call_kwargs = mock_post.call_args
        assert call_args[0] == peer_url
        assert call_kwargs["json"] == {"token": mock_new_tok}
        assert "Bearer" in call_kwargs["headers"]["Authorization"]


def test_api_auth_token_security(test_client):
    """Endpoint /api/auth/token requires valid ADMIN_TOKEN Bearer authentication."""
    # 1. Missing Authorization header -> 401
    r1 = test_client.get("/api/auth/token")
    assert r1.status_code == 401

    # 2. Wrong token -> 403
    r2 = test_client.get("/api/auth/token", headers={"Authorization": "Bearer wrong-secret"})
    assert r2.status_code == 403

    # 3. Valid admin token -> 200
    admin_secret = os.getenv("ADMIN_TOKEN") or "finplus-admin-secret-2026"
    r3 = test_client.get("/api/auth/token", headers={"Authorization": f"Bearer {admin_secret}"})
    assert r3.status_code == 200
    data = r3.json()
    assert data["status"] == "SUCCESS"
    assert "token" in data
    assert data["role"] == "master"
