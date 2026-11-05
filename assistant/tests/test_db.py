"""DB 계층: 시각 칸, 옛 SQLite 파일 고치기, 인덱스, 읽기 전용 연결 (실제 서버 없음)"""
import os, sqlite3, subprocess, sys, tempfile, unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

from clients import db
from core import apikeys, config, users
from features import notes, schedule

KST = ZoneInfo("Asia/Seoul")
OLD_SCHEMA = """
CREATE TABLE people (id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL);
CREATE TABLE links (source TEXT NOT NULL, external_id TEXT NOT NULL, user TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY (source, external_id));
CREATE TABLE settings (user TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY (user, key));
CREATE TABLE admin_log (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT, detail TEXT);
CREATE TABLE api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, hash TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL, last_used TEXT);
CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, text TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, at TEXT NOT NULL, text TEXT NOT NULL, created TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0);
"""


class Base(unittest.TestCase):
    def setUp(self):
        if os.getenv("TEST_POSTGRES"):
            self.skipTest("SQLite 전용 시험")
        self.real_db, config.DB_PATH = config.DB_PATH, os.path.join(tempfile.mkdtemp(), "d.db")

    def tearDown(self):
        config.DB_PATH = self.real_db


class Times(Base):
    def test_text_is_kst_iso_and_read_back_as_aware(self):
        eid = schedule.add("a", datetime(2026, 11, 2, 15, 0, tzinfo=KST), "치과")
        with sqlite3.connect(config.DB_PATH) as c:
            at = c.execute("SELECT at FROM events WHERE id=?", (eid,)).fetchone()[0]
        self.assertEqual(at, "2026-11-02T15:00:00+09:00")
        got = schedule.get("a", eid)[1]
        self.assertEqual(got, datetime(2026, 11, 2, 15, 0, tzinfo=KST))
        self.assertIsNotNone(got.tzinfo)

    def test_other_zone_is_saved_as_kst(self):
        eid = schedule.add("a", datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc), "회의")
        with sqlite3.connect(config.DB_PATH) as c:
            self.assertEqual(c.execute("SELECT at FROM events WHERE id=?", (eid,)).fetchone()[0], "2026-11-02T15:00:00+09:00")

    def test_on_day_keeps_the_day_boundaries(self):
        for h, m, s in ((0, 0, 0), (23, 59, 59)):
            schedule.add("a", datetime(2026, 11, 2, h, m, s, tzinfo=KST), "안")
        schedule.add("a", datetime(2026, 11, 3, 0, 0, tzinfo=KST), "밖")
        schedule.add("a", datetime(2026, 11, 1, 23, 59, tzinfo=KST), "밖")
        self.assertEqual([r[2] for r in schedule.on_day("a", date(2026, 11, 2))], ["안", "안"])

    def test_note_fmt_and_search_ignores_english_case(self):
        notes.add("a", "Docker 설정")
        self.assertEqual(len(notes.search("a", "docker")), 1)
        self.assertRegex(notes.fmt(notes.recent("a")), r"^#1 \[\d\d-\d\d \d\d:\d\d\] Docker 설정$")

    def test_since_uses_time_not_text(self):
        notes.add("a", "지금")
        with db.connect() as c:
            c.execute("UPDATE notes SET created=?", (datetime.now(KST) - timedelta(days=8),))
        self.assertEqual(notes.since("a", 7), [])


class Upgrade(Base):
    def make_old(self, at="2026-11-02 15:00:00"):
        with sqlite3.connect(config.DB_PATH) as c:
            c.executescript(OLD_SCHEMA)
            c.execute("INSERT INTO people VALUES ('owner','관리자','admin',1,'2026-10-20T02:26+09:00')")
            c.execute("INSERT INTO links VALUES ('discord','1','owner','2026-10-20T02:26+09:00')")
            c.execute("INSERT INTO settings VALUES ('owner','기본위치','판교','2026-10-20T02:26+09:00')")
            c.execute("INSERT INTO admin_log(time, actor, action) VALUES ('2026-10-20T02:26+09:00','cli','user_add')")
            c.execute("INSERT INTO api_keys(user, name, kind, hash, created) VALUES ('owner','봇','bot','h','2026-10-20T02:26+09:00')")
            c.execute("INSERT INTO notes(user, text, created) VALUES ('owner','첫','2026-10-20T02:26:30+09:00')")
            c.execute("INSERT INTO notes(user, text, created) VALUES ('owner','둘','2026-10-20T02:27:00+09:00')")
            c.execute("DELETE FROM notes WHERE id=2")
            c.execute("INSERT INTO events(user, at, text, created) VALUES ('owner',?,'치과','2026-10-20 02:26')", (at,))

    def test_old_file_is_upgraded_and_backed_up(self):
        self.make_old()
        self.assertEqual(notes.recent("owner")[0][1], "첫")
        self.assertEqual(users.get_setting("owner", "기본위치"), "판교")
        self.assertEqual(users.resolve("discord", "1"), "owner")
        self.assertEqual(apikeys.list_keys()[0]["user"], "owner")
        self.assertEqual(apikeys.list_keys()[0]["created"], datetime(2026, 10, 20, 2, 26, tzinfo=KST))
        row = schedule.upcoming("owner", current=datetime(2026, 11, 1, tzinfo=KST))[0]
        self.assertEqual(row[1:], (datetime(2026, 11, 2, 15, 0, tzinfo=KST), "치과"))
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT mode, last_offset FROM events").fetchone(), (schedule.MODE_AT_TIME, None))
        with sqlite3.connect(config.DB_PATH + ".bak-before-upgrade") as c:
            self.assertEqual(c.execute("SELECT user FROM notes").fetchone()[0], "owner")

    def test_deleted_ids_are_not_reused(self):
        self.make_old()
        self.assertEqual(notes.add("owner", "셋"), 3)

    def test_old_news_subs_gets_article_count(self):
        db.connect().__exit__(None, None, None)
        raw = sqlite3.connect(config.DB_PATH)
        raw.execute("DROP TABLE news_subs")
        raw.execute("CREATE TABLE news_subs (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT NOT NULL, "
                    "channel_name TEXT NOT NULL, category TEXT NOT NULL, times TEXT NOT NULL, last_slot TEXT, "
                    "turn INTEGER NOT NULL DEFAULT 0, created TIMESTAMPTZ NOT NULL)")
        raw.execute("INSERT INTO news_subs(channel_id, channel_name, category, times, created) "
                    "VALUES ('5','c','it','07:00','2026-11-03T07:00:00+09:00')")
        raw.commit()
        raw.close()
        db._ready.clear()
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT times, article_count FROM news_subs").fetchone()[:2], ("07:00", 5))

    def test_new_file_is_not_upgraded_again(self):
        notes.add("a", "메모")
        db._ready.clear()
        notes.add("a", "메모2")
        self.assertFalse(os.path.exists(config.DB_PATH + ".bak-before-upgrade"))

    def test_unreadable_time_stops_without_change(self):
        self.make_old(at="내일쯤")
        with self.assertRaises(db.DbError):
            notes.recent("owner")
        with sqlite3.connect(config.DB_PATH) as c:
            self.assertEqual(c.execute("SELECT user FROM notes").fetchone()[0], "owner")
            self.assertEqual(c.execute("SELECT at FROM events").fetchone()[0], "내일쯤")


class Indexes(Base):
    def test_due_query_uses_the_partial_index(self):
        schedule.add("a", datetime(2026, 11, 2, 15, 0, tzinfo=KST), "치과")
        with db.connect() as c:
            plan = c.execute("EXPLAIN QUERY PLAN SELECT id, user_id, at, text FROM events WHERE notified=0 AND at<=? ORDER BY at, id",
                             (datetime(2026, 11, 2, tzinfo=KST),)).fetchall()
        self.assertIn("idx_events_due", str(plan))

    def test_all_indexes_exist(self):
        with db.connect() as c:
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        self.assertLessEqual({"idx_events_due", "idx_events_user_at", "idx_notes_user_id", "idx_notes_user_created",
                              "idx_links_user", "idx_admin_log_time"}, names)


class ReadOnly(Base):
    def test_write_is_refused(self):
        notes.add("a", "메모")
        for sql in ("DELETE FROM notes", "INSERT INTO notes(user_id, text, created) VALUES ('a','b','c')",
                    "DROP TABLE notes", "WITH x AS (SELECT 1) DELETE FROM notes"):
            with db.connect_readonly() as c:
                with self.assertRaises(sqlite3.Error, msg=sql):
                    c.execute(sql)
        self.assertEqual(len(notes.recent("a")), 1)

    def test_key_hash_is_hidden(self):
        users.bootstrap([])
        apikeys.create("owner", "앱")
        with db.connect_readonly() as c:
            self.assertEqual(c.execute("SELECT id, name FROM api_keys").fetchone(), (1, "앱"))
            for sql in ("SELECT hash FROM api_keys", "SELECT * FROM api_keys", "SELECT id FROM api_keys WHERE hash LIKE 'a%'"):
                with self.assertRaises(sqlite3.Error, msg=sql):
                    c.execute(sql)

    def test_attach_is_refused(self):
        with db.connect_readonly() as c:
            with self.assertRaises(sqlite3.Error):
                c.execute("ATTACH DATABASE ':memory:' AS other")


class Backend(unittest.TestCase):
    def test_tests_never_use_postgres_by_default(self):
        if not os.getenv("TEST_POSTGRES"):
            self.assertEqual(config.DB_BACKEND, "sqlite")

    def test_real_program_keeps_its_backend_even_if_unittest_is_imported(self):
        code = "import unittest; from core import config; print(config.DB_BACKEND)"
        env = {**os.environ, "DB_BACKEND": "postgres"}
        env.pop("TEST_POSTGRES", None)
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(__file__)))
        self.assertEqual(out.stdout.strip(), "postgres", out.stderr)


if __name__ == "__main__":
    unittest.main()
