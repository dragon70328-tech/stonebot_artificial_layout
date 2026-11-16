"""License Service account, trial, order and entitlement flow tests."""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from fastapi.testclient import TestClient

from src.license_delivery import (
    CompositeCodeSender,
    DeliveryError,
    SmtpEmailSender,
    WebhookSmsSender,
)
from src.license_service import (
    CapturedCodeSender,
    LicenseService,
    create_license_app,
)
from src.licensing import EntitlementToken, verify_entitlement


def _service(tmp_path):
    private = Ed25519PrivateKey.generate()
    sender = CapturedCodeSender()
    service = LicenseService(
        tmp_path / "license.sqlite3",
        private_key=private,
        key_id="test-license-key",
        sender=sender,
        code_pepper="test-pepper",
    )
    app = create_license_app(service, admin_token="admin-secret")
    return service, sender, private, TestClient(app)


def _login(client, sender, email, installation_id):
    requested = client.post(
        "/v1/auth/request-code",
        json={
            "identity_type": "email",
            "destination": email,
        },
    )
    assert requested.status_code == 200
    code = sender.latest_code(destination=email, purpose="login")
    assert code
    verified = client.post(
        "/v1/auth/verify-code",
        json={
            "identity_type": "email",
            "destination": email,
            "code": code,
            "installation_id": installation_id,
            "display_name": "Test PC",
        },
    )
    assert verified.status_code == 200, verified.text
    return verified.json()


def test_trial_three_jobs_then_paid_activation(tmp_path):
    service, sender, private, client = _service(tmp_path)
    try:
        login = _login(
            client,
            sender,
            "user@example.com",
            "device-test-0001",
        )
        account_id = login["account_id"]
        device_id = login["device_id"]
        session_headers = {
            "Authorization": f"Bearer {login['session_token']}"
        }
        trial_token = EntitlementToken.model_validate(login["entitlement"])
        trial_status = verify_entitlement(
            trial_token,
            private.public_key(),
            expected_device_id=device_id,
        )
        assert trial_status.state == "active"
        assert trial_status.trial_remaining == 3

        no_session = client.post(
            "/v1/usage/reserve",
            json={"job_id": "job-unauthenticated"},
        )
        assert no_session.status_code == 401
        assert no_session.json()["error"]["code"] == "SESSION_REQUIRED"

        authorizations = []
        expected_remaining = [2, 1, 0]
        for index, remaining in enumerate(expected_remaining, start=1):
            reserved = client.post(
                "/v1/usage/reserve",
                headers=session_headers,
                json={
                    "job_id": f"job-{index}",
                    "input_sha256": f"{index:064x}",
                },
            )
            assert reserved.status_code == 200, reserved.text
            payload = reserved.json()
            assert payload["allowed"] is True
            assert payload["trial_remaining"] == remaining
            authorizations.append(payload["authorization_id"])
            committed = client.post(
                "/v1/usage/commit",
                headers=session_headers,
                json={
                    "authorization_id": payload["authorization_id"],
                },
            )
            assert committed.status_code == 200
            assert committed.json()["trial_remaining"] == remaining

        exhausted = client.post(
            "/v1/usage/reserve",
            headers=session_headers,
            json={
                "job_id": "job-4",
            },
        )
        assert exhausted.status_code == 403
        assert exhausted.json()["error"]["code"] == "TRIAL_EXHAUSTED"

        unauthorized = client.post(
            "/v1/admin/orders",
            json={
                "account_id": account_id,
                "plan": "monthly",
                "amount_cents": 19900,
                "currency": "CNY",
                "payment_reference": "payment-1",
            },
        )
        assert unauthorized.status_code == 401

        order_response = client.post(
            "/v1/admin/orders",
            headers={"Authorization": "Bearer admin-secret"},
            json={
                "account_id": account_id,
                "plan": "monthly",
                "amount_cents": 19900,
                "currency": "CNY",
                "payment_reference": "payment-1",
            },
        )
        assert order_response.status_code == 200, order_response.text
        order_no = order_response.json()["order_no"]
        confirmed = client.post(
            f"/v1/admin/orders/{order_no}/confirm",
            headers={"Authorization": "Bearer admin-secret"},
            json={"actor": "sales-admin"},
        )
        assert confirmed.status_code == 200, confirmed.text
        activation_code = sender.latest_code(
            destination="user@example.com",
            purpose="activation",
        )
        assert activation_code

        activated = client.post(
            "/v1/license/activate",
            headers=session_headers,
            json={
                "order_no": order_no,
                "activation_code": activation_code,
            },
        )
        assert activated.status_code == 200, activated.text
        paid_token = EntitlementToken.model_validate(
            activated.json()["entitlement"]
        )
        paid_status = verify_entitlement(
            paid_token,
            private.public_key(),
            expected_device_id=device_id,
        )
        assert paid_status.plan == "monthly"
        assert paid_status.production_allowed is True

        paid_reserve = client.post(
            "/v1/usage/reserve",
            headers=session_headers,
            json={
                "job_id": "job-4",
            },
        )
        assert paid_reserve.status_code == 200
        assert paid_reserve.json()["subscription"] is True
        assert paid_reserve.json()["entitlement"]

        accounts = client.get(
            "/v1/admin/accounts",
            headers={"Authorization": "Bearer admin-secret"},
            params={"query": "user@example.com"},
        )
        assert accounts.status_code == 200
        assert accounts.json()["items"][0]["account_id"] == account_id
        devices = client.get(
            f"/v1/admin/accounts/{account_id}/devices",
            headers={"Authorization": "Bearer admin-secret"},
        )
        assert devices.status_code == 200
        assert devices.json()["items"][0]["installation_id"] == device_id
        orders = client.get(
            "/v1/admin/orders",
            headers={"Authorization": "Bearer admin-secret"},
        )
        assert orders.status_code == 200
        assert orders.json()["items"][0]["order_no"] == order_no
        entitlements = client.get(
            f"/v1/admin/accounts/{account_id}/entitlements",
            headers={"Authorization": "Bearer admin-secret"},
        )
        assert entitlements.status_code == 200
        assert any(
            item["order_no"] == order_no
            for item in entitlements.json()["items"]
        )

        refunded = client.post(
            f"/v1/admin/orders/{order_no}/refund",
            headers={"Authorization": "Bearer admin-secret"},
            json={"actor": "refund-admin"},
        )
        assert refunded.status_code == 200
        assert refunded.json()["status"] == "refunded"
        after_refund = client.post(
            "/v1/usage/reserve",
            headers=session_headers,
            json={"job_id": "job-5"},
        )
        assert after_refund.status_code == 403
        assert after_refund.json()["error"]["code"] == "TRIAL_EXHAUSTED"

        unbound = client.post(
            f"/v1/admin/accounts/{account_id}/devices/{device_id}/unbind",
            headers={"Authorization": "Bearer admin-secret"},
            json={"actor": "support-admin"},
        )
        assert unbound.status_code == 200
        assert unbound.json()["revoked"] is True
        invalidated = client.post(
            "/v1/usage/reserve",
            headers=session_headers,
            json={"job_id": "job-6"},
        )
        assert invalidated.status_code == 401
        assert invalidated.json()["error"]["code"] == "SESSION_INVALID"
    finally:
        service.close()


def test_wrong_code_and_wrong_activation_code_are_rejected(tmp_path):
    service, sender, _private, client = _service(tmp_path)
    try:
        login = _login(
            client,
            sender,
            "second@example.com",
            "device-test-0002",
        )
        account_id = login["account_id"]
        device_id = login["device_id"]
        session_headers = {
            "Authorization": f"Bearer {login['session_token']}"
        }
        order = client.post(
            "/v1/admin/orders",
            headers={"Authorization": "Bearer admin-secret"},
            json={
                "account_id": account_id,
                "plan": "yearly",
                "amount_cents": 199900,
                "currency": "CNY",
                "payment_reference": "payment-2",
            },
        ).json()
        client.post(
            f"/v1/admin/orders/{order['order_no']}/confirm",
            headers={"Authorization": "Bearer admin-secret"},
            json={"actor": "sales-admin"},
        )
        good_code = sender.latest_code(
            destination="second@example.com",
            purpose="activation",
        )
        wrong_code = "000000" if good_code != "000000" else "000001"
        rejected = client.post(
            "/v1/license/activate",
            headers=session_headers,
            json={
                "order_no": order["order_no"],
                "activation_code": wrong_code,
            },
        )
        assert rejected.status_code == 400
        assert rejected.json()["error"]["code"] == "ACTIVATION_INVALID"
    finally:
        service.close()


def test_delivery_adapters_build_messages_and_fail_closed():
    email = SmtpEmailSender(
        host="smtp.example.com",
        port=587,
        username=None,
        password=None,
        from_address="noreply@example.com",
    )
    message = email.build_message("user@example.com", "123456", "login")
    assert message["To"] == "user@example.com"
    assert "123456" in message.get_content()
    assert "登录" in message["Subject"]

    composite = CompositeCodeSender(email_sender=None, sms_sender=None)
    try:
        composite.send_email("user@example.com", "123456", "login")
    except DeliveryError as exc:
        assert "not configured" in str(exc)
    else:
        raise AssertionError("missing email provider must fail")

    try:
        WebhookSmsSender(
            url="http://sms.example.com/send",
            bearer_token="secret",
        )
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("non-HTTPS SMS webhook must be rejected")
