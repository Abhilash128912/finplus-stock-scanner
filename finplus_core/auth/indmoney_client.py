"""
FinPlus+ INDmoney / INDstocks Enterprise Authentication Client
============================================================
Author: FinPlus+ Enterprise Infrastructure
Module: finplus_core.auth.indmoney_client
Version: 1.0.0
Standard: ISO-8601 Timestamps, Strict Typing, Zero Assumed Data

Security Architecture:
- Headless RFC 6238 TOTP authentication via pyotp.
- Automatic JWT claim parsing and expiration tracking.
- Client-side rate-limiting guard (enforces 60-second cooldown between token generations).
- Auto-refresh mechanism before 24-hour expiration threshold.
- Verified connection tests against live broker endpoints.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pyotp
import requests
from dotenv import load_dotenv

from finplus_core.utils.logger import get_finplus_logger

logger = get_finplus_logger("INDMoneyAuth")

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


class INDMoneyAuthError(Exception):
    """Base exception for INDmoney authentication failures."""
    pass


class RateLimitCooldownError(INDMoneyAuthError):
    """Raised when token generation is attempted within the 60-second broker cooldown window."""
    pass


class InvalidCredentialsError(INDMoneyAuthError):
    """Raised when broker rejects credentials (MPIN, Client ID, or TOTP)."""
    pass


class INDMoneyAuthClient:
    """Enterprise authentication client for INDmoney / INDstocks API."""

    BASE_URL = "https://api.indstocks.com"
    TOKEN_ENDPOINT = "/generate/token"
    PROFILE_ENDPOINT = "/user/profile"
    HOLDINGS_ENDPOINT = "/portfolio/holdings"
    POSITIONS_ENDPOINT = "/portfolio/positions"

    def __init__(
        self,
        client_id: Optional[str] = None,
        totp_secret: Optional[str] = None,
        mpin: Optional[str] = None,
        access_token: Optional[str] = None,
        env_file_path: Optional[str | Path] = None,
    ) -> None:
        """
        Initializes the INDmoney Authentication Client.
        If parameters are omitted, loads them from the specified .env file or environment variables.
        """
        # Load environment variables if file provided or default exists
        if env_file_path:
            load_dotenv(dotenv_path=env_file_path, override=True)
        else:
            default_env = Path(__file__).resolve().parent.parent.parent / "config" / ".env"
            if default_env.exists():
                load_dotenv(dotenv_path=default_env, override=True)
            else:
                load_dotenv(override=True)

        self.client_id = client_id or os.getenv("INDMONEY_CLIENT_ID")
        self.totp_secret = totp_secret or os.getenv("INDMONEY_TOTP_SECRET")
        self.mpin = mpin or os.getenv("INDMONEY_MPIN")
        self.access_token = access_token or os.getenv("INDMONEY_ACCESS_TOKEN")
        self.env_file_path = Path(env_file_path) if env_file_path else Path(__file__).resolve().parent.parent.parent / "config" / ".env"

        self._last_token_request_time: float = 0.0

        self._validate_config()

    def _validate_config(self) -> None:
        """Validates that necessary configuration parameters are present."""
        missing = []
        if not self.client_id:
            missing.append("INDMONEY_CLIENT_ID")
        if not self.totp_secret:
            missing.append("INDMONEY_TOTP_SECRET")
        if not self.mpin:
            missing.append("INDMONEY_MPIN")

        if missing:
            raise InvalidCredentialsError(
                f"Missing required INDmoney configuration keys: {', '.join(missing)}"
            )

    def generate_current_totp(self) -> str:
        """Generates the current 6-digit RFC 6238 TOTP code."""
        try:
            totp = pyotp.TOTP(self.totp_secret)
            return totp.now()
        except Exception as exc:
            logger.error("Failed to generate TOTP code: %s", exc)
            raise INDMoneyAuthError(f"TOTP generation failed: {exc}") from exc

    @staticmethod
    def decode_jwt_claims(token: str) -> Dict[str, Any]:
        """
        Decodes JWT payload claims without cryptographic verification to inspect expiration.
        Strictly reads standard 'exp', 'iat', 'tokenID', 'clientID'.
        """
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return {}
            payload_b64 = parts[1]
            # Handle base64 padding
            rem = len(payload_b64) % 4
            if rem > 0:
                payload_b64 += "=" * (4 - rem)
            payload_json = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
            return json.loads(payload_json)
        except Exception as exc:
            logger.warning("Failed to decode JWT claims: %s", exc)
            return {}

    def get_token_expiration(self, token: Optional[str] = None) -> Optional[datetime.datetime]:
        """Returns the expiration datetime of the token in IST timezone."""
        target_token = token or self.access_token
        if not target_token:
            return None

        claims = self.decode_jwt_claims(target_token)
        exp_timestamp = claims.get("exp")
        if not exp_timestamp:
            return None

        return datetime.datetime.fromtimestamp(exp_timestamp, tz=IST)

    def is_token_valid(self, buffer_seconds: int = 300) -> bool:
        """
        Checks whether the currently held access token is valid and not expiring
        within `buffer_seconds` (default: 5 minutes).
        """
        if not self.access_token:
            return False

        exp_dt = self.get_token_expiration(self.access_token)
        if not exp_dt:
            return False

        now_dt = datetime.datetime.now(tz=IST)
        remaining = (exp_dt - now_dt).total_seconds()
        return remaining > buffer_seconds

    def generate_fresh_token(self, persist_to_env: bool = True) -> str:
        """
        Requests a new 24-hour access token from the INDstocks API using MPIN and TOTP.
        Enforces a 60-second client-side cooldown to protect broker rate-limits.
        """
        # Check if another app in the suite already generated a valid token within the last 60 seconds
        latest_env_tok = self._reload_token_from_env()
        if latest_env_tok:
            claims = self.decode_jwt_claims(latest_env_tok)
            iat = claims.get("iat", 0)
            if (time.time() - iat) < 60.0:
                logger.info("Shared token in config/.env was issued %ds ago by another app. Reusing without broker call.", int(time.time() - iat))
                self.access_token = latest_env_tok
                return latest_env_tok

        elapsed = time.time() - self._last_token_request_time
        if elapsed < 60.0 and self._last_token_request_time > 0:
            wait_needed = int(60.0 - elapsed) + 1
            raise RateLimitCooldownError(
                f"Rate limit cooldown active. Please wait {wait_needed}s before requesting a new token."
            )

        totp_code = self.generate_current_totp()
        url = f"{self.BASE_URL}{self.TOKEN_ENDPOINT}"
        headers = {
            "x-api-key": self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "mpin": self.mpin,
            "totp": totp_code,
        }

        logger.info("Dispatching headless TOTP token request to %s...", url)
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=12)
            self._last_token_request_time = time.time()

            if resp.status_code == 200:
                data = resp.json()
                new_token = data.get("token")
                if not new_token:
                    raise INDMoneyAuthError(f"Malformed token response: {resp.text}")

                self.access_token = new_token
                claims = self.decode_jwt_claims(new_token)
                exp_dt = self.get_token_expiration(new_token)

                logger.info(
                    "Token successfully issued. Valid until: %s (Client: %s)",
                    exp_dt.isoformat() if exp_dt else "Unknown",
                    claims.get("clientID", self.client_id),
                )

                if persist_to_env and self.env_file_path:
                    self._persist_token_to_env(new_token)

                return new_token

            elif resp.status_code in (400, 401, 403):
                logger.error("Authentication rejected: %s %s", resp.status_code, resp.text)
                raise InvalidCredentialsError(
                    f"Broker rejected authentication ({resp.status_code}): {resp.text}"
                )
            elif resp.status_code == 429:
                logger.error("Broker rate limit triggered (429): %s", resp.text)
                raise RateLimitCooldownError(f"Broker rate limit hit (429): {resp.text}")
            else:
                logger.error("Token generation failed with HTTP %s: %s", resp.status_code, resp.text)
                raise INDMoneyAuthError(f"HTTP {resp.status_code}: {resp.text}")

        except requests.RequestException as exc:
            logger.error("Network error during token generation: %s", exc)
            raise INDMoneyAuthError(f"Network error: {exc}") from exc

    def _reload_token_from_env(self) -> Optional[str]:
        """Reloads the access token from config/.env to pick up tokens generated by other suite apps."""
        if not self.env_file_path or not self.env_file_path.exists():
            return None
        try:
            with open(self.env_file_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("INDMONEY_ACCESS_TOKEN="):
                        val = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                        if val and val != self.access_token:
                            self.access_token = val
                            logger.info("Synchronized fresh access token from shared config/.env")
                        return val
        except Exception as e:
            logger.warning("Could not read shared config/.env: %s", e)
        return None

    def force_refresh_token(self) -> str:
        """Forces fresh token generation via headless TOTP regardless of held token timestamp."""
        logger.info("Forcing fresh token generation via TOTP...")
        return self.generate_fresh_token(persist_to_env=True)

    def get_valid_token(self, auto_refresh: bool = True) -> str:
        """
        Returns a verified active access token.
        Always synchronizes with shared config/.env to avoid concurrent generation conflicts.
        """
        self._reload_token_from_env()

        if self.is_token_valid():
            return self.access_token

        if auto_refresh:
            logger.info("Current token expired or expiring soon. Refreshing via TOTP...")
            return self.generate_fresh_token()

        raise INDMoneyAuthError("Current access token is expired and auto_refresh is disabled.")

    def get_auth_headers(self) -> Dict[str, str]:
        """Returns standard HTTP headers for protected broker requests."""
        token = self.get_valid_token(auto_refresh=True)
        return {
            "Authorization": token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def verify_connection(self) -> Dict[str, Any]:
        """
        Performs a live health check by fetching user profile data from the broker.
        Strict verification: confirms user identity and active session.
        """
        headers = self.get_auth_headers()
        url = f"{self.BASE_URL}{self.PROFILE_ENDPOINT}"

        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code in (401, 403):
                logger.warning("Profile check returned HTTP %s. Auto-refreshing session token via headless TOTP...", resp.status_code)
                try:
                    self.generate_fresh_token(persist_to_env=True)
                    headers = self.get_auth_headers()
                    resp = requests.get(url, headers=headers, timeout=10)
                except Exception as ref_err:
                    logger.error("Auto-refresh during profile check failed: %s", ref_err)

            if resp.status_code == 200:
                payload = resp.json()
                data = payload.get("data", {})
                logger.info(
                    "Connection verified successfully. User: %s %s (ID: %s, Email: %s)",
                    data.get("first_name", ""),
                    data.get("last_name", ""),
                    data.get("user_id", ""),
                    data.get("email", ""),
                )
                return {
                    "status": "HEALTHY",
                    "timestamp": datetime.datetime.now(tz=IST).isoformat(),
                    "user_id": data.get("user_id"),
                    "email": data.get("email"),
                    "first_name": data.get("first_name"),
                    "last_name": data.get("last_name"),
                    "mobile": data.get("mobile_number"),
                }
            else:
                logger.error("Profile check returned HTTP %s: %s", resp.status_code, resp.text)
                return {
                    "status": "UNHEALTHY",
                    "http_status": resp.status_code,
                    "error": resp.text,
                    "timestamp": datetime.datetime.now(tz=IST).isoformat(),
                }
        except requests.RequestException as exc:
            logger.error("Health check network failure: %s", exc)
            return {
                "status": "OFFLINE",
                "error": str(exc),
                "timestamp": datetime.datetime.now(tz=IST).isoformat(),
            }

    def get_portfolio_holdings(self) -> List[Dict[str, Any]]:
        """
        Retrieves live portfolio holdings directly from the broker ledger.
        Guarantees zero fabricated or assumed quantities.
        """
        headers = self.get_auth_headers()
        url = f"{self.BASE_URL}{self.HOLDINGS_ENDPOINT}"
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])

    def get_portfolio_positions(self) -> List[Dict[str, Any]]:
        """
        Retrieves live open and closed intraday positions directly from the broker ledger.
        Guarantees zero fabricated or assumed values.
        """
        headers = self.get_auth_headers()
        url = f"{self.BASE_URL}{self.POSITIONS_ENDPOINT}"
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])

    def _persist_token_to_env(self, new_token: str) -> None:
        """Safely updates INDMONEY_ACCESS_TOKEN in the local .env file."""
        try:
            if not self.env_file_path.exists():
                logger.info("Cloud environment detected (no .env file at %s). Active token stored safely in memory.", self.env_file_path)
                return

            with open(self.env_file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            updated = False
            new_lines = []
            for line in lines:
                if line.startswith("INDMONEY_ACCESS_TOKEN="):
                    new_lines.append(f"INDMONEY_ACCESS_TOKEN={new_token}\n")
                    updated = True
                else:
                    new_lines.append(line)

            if not updated:
                new_lines.append(f"INDMONEY_ACCESS_TOKEN={new_token}\n")

            with open(self.env_file_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

            logger.info("Updated INDMONEY_ACCESS_TOKEN in %s", self.env_file_path)
        except Exception as exc:
            logger.error("Failed to persist token to %s: %s", self.env_file_path, exc)

