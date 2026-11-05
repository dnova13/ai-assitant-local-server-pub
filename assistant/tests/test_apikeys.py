"""API 키 발급·검증·폐기 (네트워크 없음)"""
import os, sqlite3, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

from core import apikeys, config, users


class Keys(unittest.TestCase):
    def setUp(self):
        self.real_db, config.DB_PATH = config.DB_PATH, os.path.join(tempfile.mkdtemp(), "k.db")
        users.bootstrap([])
        users.add("owner", "family1", "가족")

    def tearDown(self):
        config.DB_PATH = self.real_db

    def test_create_and_verify(self):
        key = apikeys.create("owner", "내 앱")
        self.assertTrue(key.startswith("lai_"))
        k = apikeys.verify(key)
        self.assertEqual((k["user"], k["kind"], k["name"]), ("owner", "app", "내 앱"))
        self.assertIsNotNone(apikeys.list_keys()[0]["last_used"])

    def test_plain_key_is_not_stored(self):
        key = apikeys.create("owner", "내 앱")
        with sqlite3.connect(config.DB_PATH) as c:
            dump = "\n".join(c.iterdump())
        self.assertNotIn(key, dump)

    def test_wrong_or_empty_key(self):
        apikeys.create("owner", "내 앱")
        for bad in (None, "", "lai_틀림", "다른키"):
            self.assertIsNone(apikeys.verify(bad), bad)

    def test_revoke(self):
        key = apikeys.create("owner", "내 앱")
        kid = apikeys.list_keys()[0]["id"]
        self.assertTrue(apikeys.revoke(kid))
        self.assertIsNone(apikeys.verify(key))
        self.assertFalse(apikeys.revoke(kid))  # 이미 폐기

    def test_inactive_person_key_fails(self):
        key = apikeys.create("family1", "가족 앱")
        users.deactivate("owner", "family1")
        self.assertIsNone(apikeys.verify(key))

    def test_unknown_user_and_bad_kind(self):
        with self.assertRaises(apikeys.ApiKeyError):
            apikeys.create("nobody", "x")
        with self.assertRaises(apikeys.ApiKeyError):
            apikeys.create("owner", "x", kind="admin")

    def test_cli(self):
        apikeys.main(["create", "owner", "봇", "--kind", "bot"])
        self.assertEqual(apikeys.list_keys()[0]["kind"], "bot")
        apikeys.main(["revoke", "1"])
        self.assertFalse(apikeys.list_keys()[0]["active"])


if __name__ == "__main__":
    unittest.main()
