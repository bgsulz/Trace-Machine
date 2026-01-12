import json

import pytest


class TestKofiWebhook:
    def test_valid_webhook_increments_donation(self, app):
        client = app.test_client()

        payload = {
            "verification_token": "test-token",
            "amount": "5.00",
            "type": "Donation",
        }
        resp = client.post(
            "/webhooks/kofi",
            data={"data": json.dumps(payload)},
            content_type="application/x-www-form-urlencoded",
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["added_cents"] == 500
        assert data["total_cents"] == 500

    def test_invalid_token_returns_403(self, app):
        client = app.test_client()

        payload = {
            "verification_token": "wrong-token",
            "amount": "10.00",
        }
        resp = client.post(
            "/webhooks/kofi",
            data={"data": json.dumps(payload)},
            content_type="application/x-www-form-urlencoded",
        )

        assert resp.status_code == 403

    def test_missing_token_returns_403(self, app):
        client = app.test_client()

        payload = {"amount": "10.00"}
        resp = client.post(
            "/webhooks/kofi",
            data={"data": json.dumps(payload)},
            content_type="application/x-www-form-urlencoded",
        )

        assert resp.status_code == 403

    def test_json_body_format_works(self, app):
        client = app.test_client()

        payload = {
            "verification_token": "test-token",
            "amount": "2.50",
        }
        resp = client.post(
            "/webhooks/kofi",
            json=payload,
            content_type="application/json",
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["added_cents"] == 250

    def test_malformed_json_returns_403(self, app):
        client = app.test_client()

        resp = client.post(
            "/webhooks/kofi",
            data={"data": "not valid json"},
            content_type="application/x-www-form-urlencoded",
        )

        assert resp.status_code == 403

    def test_multiple_donations_accumulate(self, app):
        client = app.test_client()

        for amount in ["5.00", "3.50", "1.50"]:
            payload = {
                "verification_token": "test-token",
                "amount": amount,
            }
            client.post(
                "/webhooks/kofi",
                data={"data": json.dumps(payload)},
                content_type="application/x-www-form-urlencoded",
            )

        payload = {
            "verification_token": "test-token",
            "amount": "0.00",
        }
        resp = client.post(
            "/webhooks/kofi",
            data={"data": json.dumps(payload)},
            content_type="application/x-www-form-urlencoded",
        )

        data = resp.get_json()
        assert data["total_cents"] == 1000  # 500 + 350 + 150


class TestParseAmountToCents:
    def test_valid_amount(self):
        from veracity.config_service import parse_amount_to_cents

        assert parse_amount_to_cents("5.00") == 500
        assert parse_amount_to_cents("0.99") == 99
        assert parse_amount_to_cents("100") == 10000

    def test_rounds_half_up(self):
        from veracity.config_service import parse_amount_to_cents

        assert parse_amount_to_cents("1.005") == 101
        assert parse_amount_to_cents("1.004") == 100

    def test_empty_returns_zero(self):
        from veracity.config_service import parse_amount_to_cents

        assert parse_amount_to_cents("") == 0
        assert parse_amount_to_cents(None) == 0

    def test_invalid_returns_zero(self):
        from veracity.config_service import parse_amount_to_cents

        assert parse_amount_to_cents("not-a-number") == 0
        assert parse_amount_to_cents("$5.00") == 0
