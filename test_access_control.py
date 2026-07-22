import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("CHANNEL_ID", "@test_channel")
os.environ.setdefault("GEMINI_API_KEY", "test-key")

import bot
import database as db


class SpreadSelectionTests(unittest.TestCase):
    def setUp(self):
        self.values = {}
        self.get_setting = patch.object(
            db, "get_setting", side_effect=lambda key: self.values.get(key)
        )
        self.set_setting = patch.object(
            db,
            "set_setting",
            side_effect=lambda key, value: self.values.__setitem__(key, value),
        )
        self.get_setting.start()
        self.set_setting.start()

    def tearDown(self):
        self.get_setting.stop()
        self.set_setting.stop()

    def test_only_two_different_cards_are_allowed_per_spread(self):
        first = db.claim_spread_selection(10, 100, 2)
        repeated = db.claim_spread_selection(10, 100, 2)
        second = db.claim_spread_selection(10, 100, 5)
        third = db.claim_spread_selection(10, 100, 6)

        self.assertTrue(first["allowed"] and first["is_new"])
        self.assertTrue(repeated["allowed"] and not repeated["is_new"])
        self.assertTrue(second["allowed"] and second["is_new"])
        self.assertFalse(third["allowed"])
        self.assertEqual(third["selections"], [2, 5])

    def test_a_new_spread_has_a_new_limit(self):
        db.claim_spread_selection(10, 100, 1)
        db.claim_spread_selection(10, 100, 2)
        result = db.claim_spread_selection(11, 100, 3)
        self.assertTrue(result["allowed"] and result["is_new"])


class MembershipTests(unittest.TestCase):
    def test_active_members_are_allowed(self):
        for status in ("creator", "administrator", "member"):
            with self.subTest(status=status):
                self.assertTrue(bot._member_has_channel_access(SimpleNamespace(status=status)))

    def test_non_members_are_denied(self):
        for status in ("left", "kicked"):
            with self.subTest(status=status):
                self.assertFalse(bot._member_has_channel_access(SimpleNamespace(status=status)))

    def test_restricted_member_must_still_belong_to_channel(self):
        self.assertTrue(
            bot._member_has_channel_access(
                SimpleNamespace(status="restricted", is_member=True)
            )
        )
        self.assertFalse(
            bot._member_has_channel_access(
                SimpleNamespace(status="restricted", is_member=False)
            )
        )


if __name__ == "__main__":
    unittest.main()
