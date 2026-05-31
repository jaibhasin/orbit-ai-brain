from __future__ import annotations

import asyncio
import unittest

try:
    from twilio.request_validator import RequestValidator
except ModuleNotFoundError:
    RequestValidator = None

try:
    from orbit.whatsapp_app import app, is_valid_twilio_signature, root_status
except (ModuleNotFoundError, RuntimeError):
    app = None
    is_valid_twilio_signature = None
    root_status = None


@unittest.skipIf(
    RequestValidator is None or app is None,
    "twilio or whatsapp_app dependencies are not installed",
)
class WhatsAppAppTests(unittest.TestCase):
    def test_routes_are_registered(self):
        paths = {route.path for route in app.routes}

        self.assertIn("/", paths)
        self.assertIn("/twilio/whatsapp", paths)
        self.assertIn("/api/whatsapp/inbound", paths)

    def test_root_status_payload(self):
        payload = asyncio.run(root_status())

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["docs_url"], "/docs")
        self.assertIn("/twilio/whatsapp", payload["webhook_paths"])
        self.assertIn("/api/whatsapp/inbound", payload["webhook_paths"])

    def test_openapi_uses_xml_for_webhook_success_responses(self):
        openapi_schema = app.openapi()

        twilio_post = openapi_schema["paths"]["/twilio/whatsapp"]["post"]
        alias_post = openapi_schema["paths"]["/api/whatsapp/inbound"]["post"]

        self.assertEqual(twilio_post["summary"], "Twilio WhatsApp inbound webhook")
        self.assertEqual(alias_post["summary"], "Legacy WhatsApp inbound webhook alias")
        self.assertEqual(twilio_post["operationId"], "twilio_whatsapp_webhook")
        self.assertEqual(alias_post["operationId"], "api_whatsapp_inbound_webhook")

        twilio_content = twilio_post["responses"]["200"]["content"]
        alias_content = alias_post["responses"]["200"]["content"]

        self.assertIn("application/xml", twilio_content)
        self.assertIn("application/xml", alias_content)
        self.assertNotIn("application/json", twilio_content)
        self.assertNotIn("application/json", alias_content)

    def test_twilio_signature_validation_accepts_signed_request(self):
        url = "https://orbit.example.com/twilio/whatsapp"
        params = {
            "From": "whatsapp:+15551234567",
            "Body": "hello orbit",
        }
        auth_token = "test-auth-token"
        signature = RequestValidator(auth_token).compute_signature(url, params)

        self.assertTrue(
            is_valid_twilio_signature(url, params, signature, auth_token)
        )

    def test_twilio_signature_validation_rejects_bad_signature(self):
        url = "https://orbit.example.com/twilio/whatsapp"
        params = {
            "From": "whatsapp:+15551234567",
            "Body": "hello orbit",
        }

        self.assertFalse(
            is_valid_twilio_signature(
                url,
                params,
                "bad-signature",
                "test-auth-token",
            )
        )


if __name__ == "__main__":
    unittest.main()
