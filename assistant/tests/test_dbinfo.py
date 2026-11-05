"""DB 보기: 명령어, 권한, DM 제한, 읽기 전용 SQL, 문장 분류 (실제 모델 없음)"""
import os, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("ASSISTANT_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

from clients import db
from core import apikeys, assistant, config, users
from entrances.discord_rules import split_message
from features import dbinfo, notes, sysadmin


class Base(unittest.TestCase):
    def setUp(self):
        self.real_db, config.DB_PATH = config.DB_PATH, os.path.join(tempfile.mkdtemp(), "i.db")
        self.bot = assistant.Assistant()
        users.bootstrap([])
        notes.add("owner", "치과 예약")
        apikeys.create("owner", "앱")

    def tearDown(self):
        config.DB_PATH = self.real_db

    def say(self, text, **kw):
        return self.bot.handle("owner", text, **kw)


class Status(Base):
    def test_status_has_a_db_line(self):
        self.assertRegex(dbinfo.status_line(), r"^- DB: (SQLite|PostgreSQL) 정상, 크기 ")
        with mock.patch.object(sysadmin, "ollama_models", return_value=[]), mock.patch.object(sysadmin, "gpu_status", return_value=[]):
            self.assertIn("- DB: ", sysadmin.get_system_status())

    def test_broken_db_does_not_break_status(self):
        with mock.patch.object(db, "connect", side_effect=db.DbError("꺼짐")):
            self.assertEqual(dbinfo.status_line(), "- DB: 연결 안 됨 (DbError)")


class Commands(Base):
    def test_table_list(self):
        out = self.say("!DB 테이블")
        self.assertIn("- notes: 1행", out)
        self.assertEqual(self.say("!db"), out)
        self.assertEqual(self.say("!디비"), out)

    def test_table_structure_marks_hidden_column(self):
        self.assertIn("user_id", self.say("!DB 테이블 events"))
        self.assertEqual(self.say("!DB 테이블 일정"), self.say("!DB 테이블 events"))
        self.assertIn("hash", self.say("!DB 테이블 api_keys"))
        self.assertIn("조회 안 됨", self.say("!DB 테이블 api_keys"))

    def test_unknown_table_is_refused(self):
        for bad in ("없는것", "pg_authid", "notes;DROP"):
            self.assertIn("테이블은 없어요", self.say(f"!DB 테이블 {bad}"))
            self.assertIn("테이블은 없어요", self.say(f"!DB 보기 {bad}"))

    def test_view_rows(self):
        out = self.say("!DB 보기 notes 5")
        self.assertIn("치과 예약", out)
        self.assertIn("notes 최근 1행", out)

    def test_view_never_shows_key_hash(self):
        out = self.say("!DB 보기 api_keys")
        self.assertIn("앱", out)
        self.assertNotIn("hash", out)
        with db.connect() as c:
            self.assertNotIn(c.execute("SELECT hash FROM api_keys").fetchone()[0], out)

    def add_notes(self, count):
        for i in range(count):
            notes.add("owner", f"메모{i}")

    def test_view_limit_is_capped(self):
        self.add_notes(120)
        out = self.say("!DB 보기 notes 999")
        self.assertIn("최근 100행", out)
        self.assertIn("100행까지", out)

    def test_view_usage(self):
        self.assertIn("사용법", self.say("!DB 보기"))
        self.assertIn("사용법", self.say("!DB 지워"))

    def test_table_name_alone_shows_rows(self):
        self.add_notes(15)
        self.assertIn("notes 최근 10행", self.say("!DB notes"))
        self.assertIn("notes 최근 10행", self.say("!db 메모"))
        self.assertIn("notes 최근 3행", self.say("!DB notes 3"))

    def test_words_after_the_table_only_give_the_count(self):
        self.add_notes(15)
        self.assertIn("notes 최근 10행", self.say("!db notes 데이터 보여줘"))
        self.assertIn("notes 최근 16행", self.say("!db notes 데이터 100개만 보여줘"))
        self.assertIn("notes 최근 2행", self.say("!DB 보기 notes 2개"))

    def test_columns(self):
        for text in ("!db people 컬럼", "!db people 컬럼 알려줘", "!db 컬럼 people", "!db people 칼럼", "!db people 테이블 컬럼 뭐야",
                     "!DB 사용자 칸", "!db people 구조"):
            out = self.say(text)
            self.assertIn("people: 1행", out, text)
            self.assertIn("- name: ", out, text)

    def test_columns_give_copy_blocks(self):
        out = self.say("!db people 컬럼")
        self.assertIn("```text\nid, name, role, active, created\n```", out)
        self.assertIn("```sql\n!SQL SELECT id, name, role, active, created FROM people LIMIT 10\n```", out)

    def test_columns_copy_query_only_where_rows_show(self):
        out = self.say("!db people 컬럼", show_rows=False)
        self.assertIn("id, name, role, active, created", out)
        self.assertNotIn("!SQL", out)

    def test_columns_copy_query_runs_on_every_table(self):
        for table in db.TABLES:
            out = self.say(f"!db {table} 컬럼")
            query = next(line for line in out.splitlines() if line.startswith("!SQL"))
            self.assertNotIn("실행 실패", self.say(query), table)

    def test_columns_copy_blocks_leave_out_hidden(self):
        out = self.say("!db api_keys 컬럼")
        names = out.split("```text\n")[1].split("\n```")[0]
        self.assertNotIn("hash", names)
        query = next(line for line in out.splitlines() if line.startswith("!SQL"))
        self.assertNotIn("hash", query)
        self.assertIn("(조회 안 됨)", out)

    def test_columns_work_anywhere(self):
        self.assertIn("- name: ", self.say("!db people 컬럼", show_rows=False))

    def test_data_words_show_rows(self):
        self.assertIn("people 최근 1행", self.say("!db people 데이터 알려줘"))
        self.assertIn("people 최근 1행", self.say("!db 테이블 people 데이터"))

    def test_columns_of_unknown_table(self):
        self.assertIn("테이블은 없어요", self.say("!db 없는것 컬럼"))
        self.assertIn("사용법", self.say("!db 컬럼"))

    def test_table_structure_ignores_trailing_words(self):
        self.assertIn("api_keys: 1행", self.say("!DB 테이블 api_keys 목록"))
        self.assertIn("- notes: 1행", self.say("!DB 테이블 목록"))

    def test_rows_by_table_name_are_limited_like_view(self):
        self.assertEqual(self.say("!DB notes", show_rows=False), dbinfo.ROWS_PLACE_ONLY)

    def test_admin_only(self):
        for text in ("!DB 테이블", "!DB 보기 notes", "!SQL SELECT 1", "!디비"):
            self.assertEqual(self.say(text, allow_admin=False), "이 명령은 관리자만 쓸 수 있어요.")

    def test_rows_only_where_allowed(self):
        for text in ("!DB 보기 notes", "!SQL SELECT text FROM notes"):
            out = self.say(text, show_rows=False)
            self.assertEqual(out, dbinfo.ROWS_PLACE_ONLY)
        self.assertIn("notes", self.say("!DB 테이블", show_rows=False))


class Sql(Base):
    def test_select(self):
        out = self.say("!SQL SELECT id, text FROM notes")
        self.assertIn("결과 1행", out)
        self.assertIn("치과 예약", out)

    def test_code_block_and_new_line(self):
        self.assertIn("치과 예약", self.say("!SQL ```sql\nSELECT text FROM notes;\n```"))
        self.assertIn("치과 예약", self.say("!SQL\nSELECT text\nFROM notes"))

    def test_question_mark_and_percent_stay_as_written(self):
        self.assertIn("?%", self.say("!SQL SELECT '?%' AS mark"))

    def test_only_reads(self):
        for sql in ("DELETE FROM notes", "DROP TABLE notes", "UPDATE notes SET text='x'", "INSERT INTO notes VALUES (1)", "ATTACH 'x' AS y"):
            self.assertIn("조회만", self.say(f"!SQL {sql}"), sql)
        self.assertEqual(len(notes.recent("owner")), 1)

    def test_one_statement(self):
        self.assertIn("한 문장", self.say("!SQL SELECT 1; DELETE FROM notes"))
        self.assertIn("주석", self.say("!SQL SELECT 1 -- x"))
        self.assertIn("한 번에", dbinfo.clean_sql("SELECT ';'; SELECT 2")[1])
        self.assertEqual(dbinfo.clean_sql("SELECT ';' ;")[0], "SELECT ';'")

    def test_write_in_with_is_refused_by_the_db(self):
        self.assertIn("실행 실패", self.say("!SQL WITH x AS (SELECT 1) DELETE FROM notes"))
        self.assertEqual(len(notes.recent("owner")), 1)

    def test_key_hash_is_refused(self):
        for sql in ("SELECT hash FROM api_keys", "SELECT * FROM api_keys"):
            out = self.say(f"!SQL {sql}")
            self.assertIn("실행 실패", out)
            self.assertIn("hash 칸은 볼 수 없어요", out)
        self.assertIn("결과 1행", self.say("!SQL SELECT id, name FROM api_keys"))

    def test_syntax_error_is_explained(self):
        self.assertIn("실행 실패", self.say("!SQL SELECT FROM WHERE"))

    def test_syntax_error_on_keys_has_no_hash_hint(self):
        out = self.say("!sql select created form api_keys")
        self.assertIn("실행 실패", out)
        self.assertNotIn("hash", out)

    def test_missing_column_points_to_the_column_command(self):
        out = self.say("!sql select created_at from api_keys")
        self.assertIn("!DB <테이블> 컬럼", out)
        self.assertNotIn("hash", out)

    def test_row_limit(self):
        for i in range(120):
            notes.add("owner", f"m{i}")
        self.assertIn(f"{dbinfo.MAX_SQL_ROWS}행까지만", self.say("!SQL SELECT id FROM notes"))

    def test_long_output_is_cut_at_the_end(self):
        for i in range(100):
            notes.add("owner", "가" * 300)
        self.assertLess(len(dbinfo.run_sql("SELECT id, text, text, text, text FROM notes")), dbinfo.MAX_OUT + 100)

    def test_long_output_splits_into_valid_code_blocks(self):
        for i in range(100):
            notes.add("owner", "가" * 300)
        chunks = split_message(self.say("!SQL SELECT id, text FROM notes"))
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 2000)
            self.assertEqual(sum(line.startswith("```") for line in chunk.split("\n")) % 2, 0, chunk[:50])


class Sentences(Base):
    """서버·DB 조회는 명령어로만. 문장은 DB 를 건드리지 않고 평소 흐름으로 간다"""
    def test_db_sentences_go_to_the_normal_flow(self):
        for text in ("DB 테이블 보여줘", "people 데이터 보여줘", "people 컬럼 알려줘", "SELECT id FROM notes", "DELETE FROM notes"):
            with mock.patch.object(self.bot, "_normal_sentence", return_value="평소") as normal:
                self.assertEqual(self.say(text), "평소", text)
            normal.assert_called_once()
        self.assertEqual(len(notes.recent("owner")), 1)

    def test_no_model_is_used_for_commands(self):
        with mock.patch.object(assistant.llm, "chat", side_effect=AssertionError), \
             mock.patch.object(assistant.classifier, "ask_system", side_effect=AssertionError):
            for text in ("!db people 데이터 알려줘", "!db people 컬럼 알려줘", "!db 테이블", "!sql SELECT 1"):
                self.say(text)


class Entrance(Base):
    def test_internal_show_rows_is_only_true_when_sent_as_true(self):
        try:
            from fastapi.testclient import TestClient
        except (ImportError, RuntimeError):  # TestClient 는 httpx 가 있어야 한다 (requirements.txt 에는 없음)
            self.skipTest("TestClient 를 쓸 수 없어서 건너뜀")
        from entrances import api_server
        from entrances.api import common
        common.LOCAL_HOSTS.add("testclient")
        self.addCleanup(common.LOCAL_HOSTS.discard, "testclient")
        users.link("cli", "owner", "discord", "777")
        key = apikeys.create("owner", "봇", kind="bot")
        client = TestClient(api_server.app)
        headers = {"Authorization": f"Bearer {key}"}
        def send(**extra):
            body = {"source": "discord", "external_id": "777", "text": "!DB 보기 notes", **extra}
            return client.post("/internal/message", json=body, headers=headers).json()["reply"]
        self.assertEqual(send(), dbinfo.ROWS_PLACE_ONLY)
        self.assertEqual(send(show_rows="true"), dbinfo.ROWS_PLACE_ONLY)
        self.assertIn("치과 예약", send(show_rows=True))

    def test_api_client_sends_show_rows_only_when_set(self):
        from clients import api_client
        with mock.patch.object(api_client, "_post", return_value={"reply": "x"}) as post:
            api_client.send("discord", 1, "a")
            self.assertNotIn("show_rows", post.call_args[0][1])
            api_client.send("discord", 1, "a", show_rows=True)
            self.assertIs(post.call_args[0][1]["show_rows"], True)


if __name__ == "__main__":
    unittest.main()
