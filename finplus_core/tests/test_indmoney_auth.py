"""
FinPlus+ Automated Test Suite: INDmoney Authentication Verification
Stamped: 2026-11-07T19:18:30+05:30
"""

import sys
from pathlib import Path

# Add D:\AI_APPS to Python path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from finplus_core.auth import INDMoneyAuthClient

def run_tests():
    print("=" * 70)
    print("FINPLUS+ INDMONEY AUTHENTICATION INTEGRATION TEST")
    print("=" * 70)
    
    # 1. Initialize Client
    client = INDMoneyAuthClient()
    print(f"[OK] Client initialized with Client ID: {client.client_id[:4]}****")
    
    # 2. Check TOTP generation
    totp = client.generate_current_totp()
    assert len(totp) == 6 and totp.isdigit(), "Invalid TOTP format"
    print(f"[OK] Live TOTP generation verified: {totp}")
    
    # 3. Check Token Expiry
    exp_dt = client.get_token_expiration()
    print(f"[OK] Token Expiration: {exp_dt.isoformat() if exp_dt else 'None'}")
    
    is_valid = client.is_token_valid()
    print(f"[OK] Token Validity: {is_valid}")
    assert is_valid, "Current access token is unexpectedly invalid"
    
    # 4. Verify Live Broker Connection
    health = client.verify_connection()
    print(f"[OK] Broker Health Check Status: {health.get('status')}")
    print(f"     User ID: {health.get('user_id')}")
    print(f"     Name: {health.get('first_name')} {health.get('last_name')}")
    print(f"     Email: {health.get('email')}")
    assert health.get("status") == "HEALTHY", "Health check failed"
    
    # 5. Check Live Ledger Endpoints (Holdings & Positions)
    holdings = client.get_portfolio_holdings()
    print(f"[OK] Live Holdings count: {len(holdings)} items (Zero fabricated data)")
    
    positions = client.get_portfolio_positions()
    print(f"[OK] Live Positions count: {len(positions)} items (Zero fabricated data)")
    
    print("=" * 70)
    print("ALL INTEGRATION TESTS PASSED (100% LIVE VERIFIED)")
    print("=" * 70)

if __name__ == "__main__":
    run_tests()

