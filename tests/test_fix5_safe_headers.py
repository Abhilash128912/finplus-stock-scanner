"""
Acceptance Test for Item 5: Safe Headers and Secrets Sanitization
- 5a. safe_headers() forces ASCII on all header values. Emoji only in message body.
- 5b. Sending a title containing 📈 (chart emoji) delivers safely with zero latin-1 UnicodeEncodeError.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCANNER_DIR = Path(__file__).resolve().parent.parent
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))

from src.notifications.ntfy_dispatcher import safe_headers, NTFYDispatcher


def test_safe_headers_strips_emojis_from_headers():
    """Requirement 5a: Non-ASCII characters (emojis) are stripped from HTTP headers."""
    raw_headers = {
        "Title": "FinPlus Trade Alert 📈 Multi-Bagger",
        "Priority": "4",
        "Tags": "rocket,moneybag 🚀",
        "X-Custom": "ASCII_SAFE_VALUE",
    }

    cleaned = safe_headers(raw_headers, auth_token="test_token_123")

    # Title must not contain emoji
    assert "📈" not in cleaned["Title"]
    assert cleaned["Title"] == "FinPlus Trade Alert  Multi-Bagger"

    # Tags must not contain 🚀
    assert "🚀" not in cleaned["Tags"]

    # All header values must encode to latin-1 / ascii cleanly without UnicodeEncodeError
    for k, v in cleaned.items():
        v.encode("latin-1")  # Should not raise UnicodeEncodeError
        v.encode("ascii")    # Should not raise UnicodeEncodeError

    assert cleaned["Authorization"] == "Bearer test_token_123"


def test_send_title_with_chart_emoji_delivered_no_latin1_error():
    """Requirement 5b: Send title containing 📈 emoji -> delivered, no latin-1 error."""
    dispatcher = NTFYDispatcher()

    captured_headers = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        nonlocal captured_headers
        captured_headers = headers
        # Simulating standard requests behavior: headers will be encoded to latin-1 by urllib3/http.client
        for h_name, h_val in headers.items():
            h_val.encode("latin-1")  # This raises UnicodeEncodeError if non-latin-1 is present
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with patch("requests.post", side_effect=fake_post):
        # Dispatch with emoji in title
        headers = {
            "Title": "Institutional Long Breakout 📈 [RELIANCE]",
            "Priority": "4",
            "Markdown": "yes",
        }
        # Emoji in body is permitted and encoded as utf-8
        body = "RELIANCE 📈 broke out of 20-period range with 2.5x RVOL."
        delivered = dispatcher._safe_post("https://ntfy.sh/test-topic", body, headers, is_priority=True)

    assert delivered is True
    assert "📈" not in captured_headers["Title"]
    assert "Institutional Long Breakout" in captured_headers["Title"]

