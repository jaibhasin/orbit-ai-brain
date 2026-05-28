from __future__ import annotations

import unittest

from orbit.whatsapp_app import app


class WhatsAppAppTests(unittest.TestCase):
    def test_twilio_inbound_alias_is_registered(self):
        paths = {route.path for route in app.routes}

        self.assertIn("/twilio/whatsapp", paths)
        self.assertIn("/api/whatsapp/inbound", paths)


if __name__ == "__main__":
    unittest.main()
