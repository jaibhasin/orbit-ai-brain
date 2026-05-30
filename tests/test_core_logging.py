from __future__ import annotations

import logging
import os
import unittest

from orbit.core import configure_dependency_logging


class CoreLoggingTests(unittest.TestCase):
    def setUp(self):
        self._orig_level = os.environ.get("ORBIT_LOG_LEVEL")

    def tearDown(self):
        if self._orig_level is None:
            os.environ.pop("ORBIT_LOG_LEVEL", None)
        else:
            os.environ["ORBIT_LOG_LEVEL"] = self._orig_level

    def test_important_mode_suppresses_dependency_info_logs(self):
        os.environ["ORBIT_LOG_LEVEL"] = "important"
        noisy = logging.getLogger("twilio.http_client")
        noisy.setLevel(logging.DEBUG)
        noisy.addHandler(logging.StreamHandler())

        configure_dependency_logging()

        self.assertEqual(noisy.level, logging.WARNING)
        self.assertEqual(len(noisy.handlers), 0)

    def test_debug_mode_keeps_dependency_debug_level(self):
        os.environ["ORBIT_LOG_LEVEL"] = "debug"
        noisy = logging.getLogger("browser_use.agent")
        noisy.setLevel(logging.WARNING)
        noisy.addHandler(logging.StreamHandler())

        configure_dependency_logging()

        self.assertEqual(noisy.level, logging.DEBUG)
        self.assertEqual(len(noisy.handlers), 0)

    def test_error_mode_only_allows_error_logs(self):
        os.environ["ORBIT_LOG_LEVEL"] = "error"
        noisy = logging.getLogger("openai")
        noisy.setLevel(logging.WARNING)
        noisy.addHandler(logging.StreamHandler())

        configure_dependency_logging()

        self.assertEqual(noisy.level, logging.ERROR)


if __name__ == "__main__":
    unittest.main()
