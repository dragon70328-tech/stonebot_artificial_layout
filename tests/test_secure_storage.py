"""Windows DPAPI local-secret tests."""

from __future__ import annotations

import pytest

from src.secure_storage import (
    dpapi_available,
    protect_secret,
    unprotect_secret,
)


@pytest.mark.skipif(not dpapi_available(), reason="Windows DPAPI unavailable")
def test_dpapi_round_trip_without_plaintext():
    secret = b"stonebot-session-token-1234567890"
    protected = protect_secret(secret)

    assert protected != secret
    assert secret not in protected
    assert unprotect_secret(protected) == secret
