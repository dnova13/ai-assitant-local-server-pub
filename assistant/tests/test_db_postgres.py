"""실제 Postgres 시험. TEST_POSTGRES=1 과 POSTGRES_* 를 줄 때만 돈다 (평소에는 건너뛴다). 빈 시험용 DB 에서만 실행한다."""
import os, sqlite3, sys, tempfile, unittest
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

from clients import db
from core import config
from features import notes
from scripts import migrate_to_postgres as migrate
from test_db import OLD_SCHEMA

KST = ZoneInfo("Asia/Seoul")


@unittest.skipUnless(os.getenv("TEST_POSTGRES"), "실제 Postgres 시험")
class Migrate(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.psycopg = psycopg
        self.old_backend, config.DB_BACKEND = config.DB_BACKEND, "postgres"
        with db.connect() as c:
            c.execute("TRUNCATE " + ",".join(db.TABLES) + " RESTART IDENTITY")
        self.path = os.path.join(tempfile.mkdtemp(), "old.db")
        with sqlite3.connect(self.path) as c:
            c.executescript(OLD_SCHEMA)
            c.execute("INSERT INTO people VALUES ('owner','관리자','admin',1,'2026-10-20T02:26+09:00')")
            c.execute("INSERT INTO api_keys(user, name, kind, hash, created) VALUES ('owner','봇','bot','비밀','2026-10-20T02:26+09:00')")
            c.execute("INSERT INTO notes(user, text, created) VALUES ('owner','첫','2026-10-20T02:26:30+09:00')")
            c.execute("INSERT INTO notes(user, text, created) VALUES ('owner','둘','2026-10-20T02:27+09:00')")
            c.execute("DELETE FROM notes WHERE id=2")
            c.execute("INSERT INTO events(user, at, text, created) VALUES ('owner','2026-11-02 15:00','치과','2026-10-20 02:26')")
        with open(self.path, "rb") as f:
            self.original = f.read()

    def tearDown(self):
        config.DB_BACKEND = self.old_backend

    def test_copy_keeps_values_and_source(self):
        counts = migrate.migrate(self.path)
        self.assertEqual((counts["notes"], counts["events"], counts["people"]), (1, 1, 1))
        with db.connect() as c:
            row = c.execute("SELECT user_id, at, mode FROM events").fetchone()
            self.assertEqual(row, ("owner", datetime(2026, 11, 2, 15, 0, tzinfo=KST), 2))
            self.assertEqual(c.execute("SELECT current_setting('TimeZone')").fetchone()[0], "Asia/Seoul")
        self.assertEqual(notes.add("owner", "셋"), 3)  # 지운 번호를 다시 쓰지 않는다
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), self.original)

    def test_target_with_data_is_refused(self):
        migrate.migrate(self.path)
        with self.assertRaises(migrate.MigrateError):
            migrate.migrate(self.path)

    def test_failure_leaves_nothing(self):
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE events SET at='내일쯤'")
        with self.assertRaises(db.DbError):
            migrate.migrate(self.path)
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM people").fetchone()[0], 0)

    def test_readonly_role(self):
        migrate.migrate(self.path)
        with db.connect_readonly() as c:
            self.assertEqual(c.execute("SELECT name FROM api_keys").fetchone()[0], "봇")
        for sql in ("SELECT hash FROM api_keys", "SELECT * FROM api_keys", "DELETE FROM notes", "DROP TABLE notes",
                    "INSERT INTO notes(user_id, text, created) VALUES ('a','b',now())", "CREATE TABLE x(a int)"):
            with db.connect_readonly() as c:
                with self.assertRaises(self.psycopg.Error, msg=sql):
                    c.execute(sql)

    def test_readonly_cannot_turn_writes_on(self):
        migrate.migrate(self.path)
        with db.connect_readonly() as c:
            with self.assertRaises(self.psycopg.Error):
                c.raw.execute("SET TRANSACTION READ WRITE; DELETE FROM notes")
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
