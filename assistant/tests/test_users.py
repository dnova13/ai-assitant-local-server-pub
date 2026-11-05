import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

from core import config, users
from features import notes


class FreshDB(unittest.TestCase):
    """시험마다 빈 DB를 쓴다"""
    def setUp(self):
        self.real_db = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "u.db")

    def tearDown(self):
        config.DB_PATH = self.real_db


class Bootstrap(FreshDB):
    def test_creates_owner_links_and_moves_notes(self):
        notes.add("111", "옛 메모")          # 예전처럼 디스코드 ID로 저장된 메모
        notes.add("999", "다른 사람 메모")
        self.assertTrue(users.bootstrap(["111"]))
        self.assertEqual(users.resolve("discord", "111"), "owner")
        self.assertEqual(users.resolve("discord", 111), "owner")  # 숫자로 와도 같음
        self.assertIsNone(users.resolve("discord", "999"))
        self.assertTrue(users.is_admin("owner"))
        self.assertEqual([r[1] for r in notes.recent("owner")], ["옛 메모"])
        self.assertEqual([r[1] for r in notes.recent("999")], ["다른 사람 메모"])  # 연결 안 된 ID는 그대로

    def test_runs_only_once(self):
        self.assertTrue(users.bootstrap(["111"]))
        self.assertFalse(users.bootstrap(["222"]))
        self.assertIsNone(users.resolve("discord", "222"))

    def test_without_notes_table_or_ids(self):
        self.assertTrue(users.bootstrap([]))
        self.assertEqual(users.get("owner")["role"], "admin")


class Manage(FreshDB):
    def setUp(self):
        super().setUp()
        users.bootstrap(["111"])

    def test_add_link_and_isolation(self):
        users.add("owner", "family1", "가족")
        users.link("owner", "family1", "discord", "222")
        self.assertEqual(users.resolve("discord", "222"), "family1")
        self.assertFalse(users.is_admin("family1"))
        with self.assertRaises(users.UserError):
            users.link("owner", "family1", "discord", "111")  # 이미 owner 에 연결
        with self.assertRaises(users.UserError):
            users.add("owner", "family1", "중복")
        with self.assertRaises(users.UserError):
            users.add("owner", "Bad ID", "x")
        with self.assertRaises(users.UserError):
            users.link("owner", "nobody", "discord", "333")

    def test_deactivate_and_activate(self):
        users.add("owner", "family1", "가족")
        users.link("owner", "family1", "discord", "222")
        users.deactivate("owner", "family1")
        self.assertIsNone(users.resolve("discord", "222"))  # 비활성이면 못 들어옴
        users.activate("owner", "family1")
        self.assertEqual(users.resolve("discord", "222"), "family1")  # 연결은 남아 있어 되돌릴 수 있음

    def test_protections(self):
        with self.assertRaises(users.UserError):
            users.deactivate("owner", "owner")        # 자기 자신
        with self.assertRaises(users.UserError):
            users.deactivate("script", "owner")       # 마지막 관리자
        users.add("script", "admin2", "관리자2", "admin")
        users.deactivate("script", "owner")           # 관리자가 둘이면 가능
        with self.assertRaises(users.UserError):
            users.unlink("owner", "discord", "111")   # 자기 연결
        with self.assertRaises(users.UserError):
            users.add("script", "x1", "x", "boss")    # 없는 역할

    def test_list_and_log(self):
        users.add("script", "family1", "가족")
        text = users.fmt_people(users.list_people())
        self.assertIn("owner  관리자  admin  활성  (discord 1개)", text)
        self.assertIn("family1  가족  user  활성  (연결 없음)", text)
        with users._conn() as c:
            actions = [r[0] for r in c.execute("SELECT action FROM admin_log ORDER BY id")]
        self.assertEqual(actions, ["bootstrap", "add"])

    def test_settings(self):
        self.assertIsNone(users.get_setting("owner", "기본위치"))
        users.set_setting("owner", "기본위치", "판교")
        users.set_setting("owner", "기본위치", "수원")
        self.assertEqual(users.get_setting("owner", "기본위치"), "수원")
        self.assertIsNone(users.get_setting("family1", "기본위치"))

    def test_script(self):
        users.main(["add", "family1", "가족"])
        users.main(["link", "family1", "discord", "222"])
        self.assertEqual(users.resolve("discord", "222"), "family1")
        with self.assertRaises(SystemExit):
            users.main(["deactivate", "owner"])


if __name__ == "__main__":
    unittest.main()
