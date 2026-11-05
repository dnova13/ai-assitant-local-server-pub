"""비서 DB 연결. .env 의 DB_BACKEND 로 sqlite(기본) 또는 postgres 를 고른다 (README.md).
- 쿼리는 어느 쪽이든 ? 자리표시자로 쓴다. 새 번호가 필요한 INSERT 는 c.insert(...) 를 쓴다.
- 시각은 시간대가 붙은 datetime 으로 주고받는다. Postgres 는 TIMESTAMPTZ, SQLite 는 '2026-11-02T15:00:00+09:00' 글자로 저장하고
  (날짜 형식이 없어서. 시간대가 하나라 글자 순서 = 시간 순서), 직접 조회해도 KST(config.TIMEZONE)로 보이게 한다.
- 옛 SQLite 파일(user 칸, 시각 형식이 제각각)은 처음 열 때 백업하고 고친다. 읽지 못하는 시각이 있으면 고치지 않고 멈춘다.
- psycopg 는 postgres 를 쓸 때만 불러온다."""
import logging
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from core import config

log = logging.getLogger("assistant.db")
TABLES = {
    "people": "id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, "
              "created TIMESTAMPTZ NOT NULL",
    "links": "source TEXT NOT NULL, external_id TEXT NOT NULL, user_id TEXT NOT NULL, created TIMESTAMPTZ NOT NULL, "
             "PRIMARY KEY (source, external_id)",
    "settings": "user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated TIMESTAMPTZ NOT NULL, "
                "PRIMARY KEY (user_id, key)",
    "admin_log": "id {pk}, time TIMESTAMPTZ NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT, detail TEXT",
    "api_keys": "id {pk}, user_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, hash TEXT NOT NULL UNIQUE, "
                "active INTEGER NOT NULL DEFAULT 1, created TIMESTAMPTZ NOT NULL, last_used TIMESTAMPTZ",
    "notes": "id {pk}, user_id TEXT NOT NULL, text TEXT NOT NULL, created TIMESTAMPTZ NOT NULL",
    "computers": "name TEXT PRIMARY KEY, target TEXT NOT NULL, mac TEXT, created TIMESTAMPTZ NOT NULL",
    "app_computers": "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, created TIMESTAMPTZ NOT NULL, "
                     "last_seen TIMESTAMPTZ, position INTEGER NOT NULL",
    "app_file_channels": "channel_id TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL, created TIMESTAMPTZ NOT NULL, "
                         "PRIMARY KEY (channel_id, user_id)",
    "app_sent_files": "message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL, "
                      "delete_at TIMESTAMPTZ NOT NULL",
    "events": "id {pk}, user_id TEXT NOT NULL, at TIMESTAMPTZ NOT NULL, text TEXT NOT NULL, created TIMESTAMPTZ NOT NULL, "
              "notified INTEGER NOT NULL DEFAULT 0, mode INTEGER NOT NULL DEFAULT 2, last_offset INTEGER",
    "managed_channels": "channel_id TEXT PRIMARY KEY, guild TEXT NOT NULL, name TEXT NOT NULL, created TIMESTAMPTZ NOT NULL",
    "channel_messages": "message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, author_id TEXT NOT NULL, author_name TEXT NOT NULL, "
                        "text TEXT NOT NULL, link TEXT NOT NULL, at TIMESTAMPTZ NOT NULL",
    "channel_files": "id {pk}, message_id TEXT NOT NULL, channel_id TEXT NOT NULL, author_id TEXT NOT NULL, author_name TEXT NOT NULL, "
                     "filename TEXT NOT NULL, kind TEXT NOT NULL, size BIGINT NOT NULL, link TEXT NOT NULL, at TIMESTAMPTZ NOT NULL",
    "news_categories": "name TEXT PRIMARY KEY, aliases TEXT NOT NULL, created TIMESTAMPTZ NOT NULL",
    "news_sources": "id {pk}, category TEXT NOT NULL, url TEXT NOT NULL, fails INTEGER NOT NULL DEFAULT 0, "
                    "created TIMESTAMPTZ NOT NULL",
    "news_keywords": "id {pk}, category TEXT NOT NULL, text TEXT NOT NULL, created TIMESTAMPTZ NOT NULL",
    "news_excluded": "name TEXT PRIMARY KEY, created TIMESTAMPTZ NOT NULL",
    "news_subs": "id {pk}, channel_id TEXT NOT NULL, channel_name TEXT NOT NULL, category TEXT NOT NULL, times TEXT NOT NULL, "
                 "last_slot TEXT, turn INTEGER NOT NULL DEFAULT 0, created TIMESTAMPTZ NOT NULL, "
                 "article_count INTEGER NOT NULL DEFAULT 5",
    "news_sent": "channel_id TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL, sent TIMESTAMPTZ NOT NULL, "
                 "PRIMARY KEY (channel_id, category, title)",
}
INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_events_due ON events(at) WHERE notified=0",  # 10초마다 도는 알림 조회. 안 보낸 일정만 담는다
    "CREATE INDEX IF NOT EXISTS idx_events_user_at ON events(user_id, at)",  # 일정 목록, 그날의 일정
    "CREATE INDEX IF NOT EXISTS idx_notes_user_id ON notes(user_id, id)",  # 메모 목록
    "CREATE INDEX IF NOT EXISTS idx_notes_user_created ON notes(user_id, created)",  # 최근 7일 요약
    "CREATE INDEX IF NOT EXISTS idx_links_user ON links(user_id)",  # 사람 -> 입구 ID
    "CREATE INDEX IF NOT EXISTS idx_admin_log_time ON admin_log(time)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_app_computers_name ON app_computers(user_id, name)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_news_subs_channel ON news_subs(channel_id, category)",  # 채널마다 카테고리 하나
    "CREATE INDEX IF NOT EXISTS idx_channel_messages_at ON channel_messages(channel_id, at)",  # 채널 관리: 최근 글 찾기
    "CREATE INDEX IF NOT EXISTS idx_channel_files_at ON channel_files(channel_id, at)",
    "CREATE INDEX IF NOT EXISTS idx_channel_files_message ON channel_files(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_news_sent_time ON news_sent(sent)",  # 오래된 게시 기록 지우기
)
# 이미 만든 Postgres 표에 없는 칸을 더한다 (SQLite 는 _needs_upgrade 가 처리한다)
POSTGRES_UPGRADES = ("ALTER TABLE news_subs ADD COLUMN IF NOT EXISTS article_count INTEGER NOT NULL DEFAULT 5",)
TIME_COLUMNS = {"people": ("created",), "links": ("created",), "settings": ("updated",), "admin_log": ("time",),
                "api_keys": ("created", "last_used"), "notes": ("created",), "events": ("at", "created"),
                "computers": ("created",), "app_computers": ("created", "last_seen"), "app_file_channels": ("created",), "app_sent_files": ("delete_at",), "managed_channels": ("created",), "channel_messages": ("at",),
                "channel_files": ("at",), "news_categories": ("created",), "news_sources": ("created",),
                "news_keywords": ("created",), "news_excluded": ("created",), "news_subs": ("created",),
                "news_sent": ("sent",)}
ID_TABLES = ("admin_log", "api_keys", "notes", "events", "channel_files", "news_sources", "news_keywords", "news_subs")  # 번호가 자동으로 늘어나는 테이블
HIDDEN_COLUMNS = {"api_keys": {"hash"}, "computers": {"target", "mac"}, "channel_messages": {"text"}}  # 읽기 전용 조회에서 보여 주지 않는 칸
READONLY_SECONDS = 5


class DbError(Exception):
    """연결·설정 문제 (안내 문구. 주소와 비밀번호는 넣지 않는다)."""


def zone():
    return ZoneInfo(config.TIMEZONE)


def local(dt):
    """시간대가 붙은 KST datetime 으로. 시간대가 없으면 KST 로 본다."""
    return dt.replace(tzinfo=zone()) if dt.tzinfo is None else dt.astimezone(zone())


def to_text(dt):
    return local(dt).isoformat(timespec="seconds")


def parse_text(value):
    """옛 형식까지 읽는다: '2026-11-02 15:00', '2026-11-02 15:00:00', '2026-11-02T02:26+09:00'. 못 읽으면 ValueError."""
    return local(datetime.fromisoformat(value))


sqlite3.register_adapter(datetime, to_text)
sqlite3.register_converter("TIMESTAMPTZ", lambda raw: parse_text(raw.decode("utf-8")))


def backend():
    return config.DB_BACKEND


class Conn:
    """with 로 쓴다: 끝나면 저장하고(오류면 되돌리고) 닫는다. 두 DB의 차이를 여기서 가린다."""

    def __init__(self, raw, kind, release):
        self.raw, self.kind, self._release = raw, kind, release

    def execute(self, sql, params=()):
        if self.kind == "postgres":
            return self.raw.execute(sql.replace("?", "%s"), params or None)
        return self.raw.execute(sql, params)

    def insert(self, sql, params=()):
        """INSERT 하고 새 번호를 돌려준다."""
        if self.kind == "postgres":
            return self.execute(f"{sql} RETURNING id", params).fetchone()[0]
        return self.execute(sql, params).lastrowid

    def columns(self, table):
        """[(칸 이름, 형식)]"""
        if self.kind == "postgres":
            return self.execute("SELECT column_name, data_type FROM information_schema.columns "
                                "WHERE table_schema='public' AND table_name=? ORDER BY ordinal_position", (table,)).fetchall()
        return [(r[1], r[2]) for r in self.execute(f"PRAGMA table_info({table})").fetchall()]

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        try:
            self.raw.commit() if kind is None else self.raw.rollback()
        finally:
            self._release()
        return False


_ready = set()
_ready_lock = threading.Lock()


def _create_all(c, pk):
    for name, columns in TABLES.items():
        c.execute(f"CREATE TABLE IF NOT EXISTS {name} ({columns.format(pk=pk)})")
    for sql in INDEXES:
        c.execute(sql)


# --- SQLite ---
def _sqlite_raw(path):
    return sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)


def _needs_upgrade(c, table):
    info = c.execute(f"PRAGMA table_info({table})").fetchall()
    if not info:
        return False
    types = {r[1]: r[2] for r in info}
    wanted = {part.split()[0] for part in re.sub(r",\s*PRIMARY KEY \(.*\)$", "", TABLES[table]).split(", ")}
    return not wanted <= set(types) or any(types[col] != "TIMESTAMPTZ" for col in TIME_COLUMNS[table] if col in types)


def _upgrade_table(c, table):
    """옛 테이블을 새 정의로 옮긴다: user -> user_id, 시각은 KST 형식, 없던 칸은 기본값. 지운 번호를 다시 쓰지 않게 번호 카운터도 옮긴다."""
    old = f"{table}_old_upgrade"
    c.execute(f"ALTER TABLE {table} RENAME TO {old}")
    c.execute(f"CREATE TABLE {table} ({TABLES[table].format(pk='INTEGER PRIMARY KEY AUTOINCREMENT')})")
    source = [r[1] for r in c.execute(f"PRAGMA table_info({old})").fetchall()]
    target = ["user_id" if name == "user" else name for name in source]
    place = ",".join("?" * len(source))
    for row in c.execute(f"SELECT {','.join(source)} FROM {old}").fetchall():
        values = list(row)
        for i, name in enumerate(target):
            if name in TIME_COLUMNS[table] and isinstance(values[i], str):
                try:
                    values[i] = to_text(parse_text(values[i]))
                except ValueError:
                    raise DbError(f"{table}.{name} 의 시각을 읽지 못해 DB 를 고치지 않았어요. 값: {values[i]!r}")
        c.execute(f"INSERT INTO {table}({','.join(target)}) VALUES ({place})", values)
    if table in ID_TABLES:
        seq = c.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (old,)).fetchone()
        if seq:
            c.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
            c.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?,?)", (table, seq[0]))
    c.execute(f"DROP TABLE {old}")


def _prepare_sqlite(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    existed = Path(path).exists()
    raw = sqlite3.connect(path, isolation_level=None)
    try:
        old = [t for t in TABLES if _needs_upgrade(raw, t)]
        if old and existed:
            shutil.copy2(path, f"{path}.bak-before-upgrade")
        raw.execute("BEGIN IMMEDIATE")
        try:
            for table in old:
                _upgrade_table(raw, table)
            _create_all(raw, "INTEGER PRIMARY KEY AUTOINCREMENT")
            raw.execute("COMMIT")
        except BaseException:
            raw.execute("ROLLBACK")
            raise
    finally:
        raw.close()


def _connect_sqlite():
    path = config.DB_PATH
    with _ready_lock:
        if path not in _ready or not Path(path).exists():
            _prepare_sqlite(path)
            _ready.add(path)
    raw = _sqlite_raw(path)
    return Conn(raw, "sqlite", raw.close)


# --- Postgres ---
_pool = None
_pool_lock = threading.Lock()


def _pg_options():
    return {"host": config.POSTGRES_HOST, "port": config.POSTGRES_PORT, "dbname": config.POSTGRES_DB,
            "options": f"-c timezone={config.TIMEZONE}"}


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            if not config.POSTGRES_PASSWORD:
                raise DbError("POSTGRES_PASSWORD 가 없어요. .env 를 확인해 주세요.")
            try:
                from psycopg_pool import ConnectionPool
            except ImportError:
                raise DbError("psycopg 가 없어요. 저장소 맨 위에서 pip install -r requirements.txt 를 실행해 주세요.")
            _pool = ConnectionPool(conninfo="", min_size=1, max_size=5, timeout=5, open=True,
                                   check=ConnectionPool.check_connection,  # DB 를 다시 켠 뒤 끊어진 연결을 쓰지 않게
                                   kwargs={**_pg_options(), "user": config.POSTGRES_USER, "password": config.POSTGRES_PASSWORD})
    return _pool


def readonly_columns(c, table):
    """읽기 전용 계정에 보여 줄 칸."""
    return [name for name, _ in c.columns(table) if name not in HIDDEN_COLUMNS.get(table, ())]


def _grant_readonly(c):
    """읽기 전용 계정이 있으면 표에 읽기 권한을 준다 (새로 생긴 표도). 숨김 칸은 주지 않는다. 실패해도 비서는 계속 돈다."""
    from psycopg import Error, sql
    try:
        if c.execute("SELECT 1 FROM pg_roles WHERE rolname=?", (config.POSTGRES_RO_USER,)).fetchone():
            role = sql.Identifier(config.POSTGRES_RO_USER)
            for table in TABLES:
                columns = sql.SQL(", ").join(sql.Identifier(n) for n in readonly_columns(c, table))
                c.raw.execute(sql.SQL("GRANT SELECT ({}) ON {} TO {}").format(columns, sql.Identifier(table), role))
        c.raw.commit()
    except Error as e:
        c.raw.rollback()
        log.warning("읽기 전용 계정 권한 설정 실패: %s", type(e).__name__)


def _connect_postgres():
    pool = _get_pool()
    try:
        raw = pool.getconn()
    except Exception as e:  # 주소가 들어갈 수 있는 원문은 넣지 않는다
        raise DbError(f"Postgres 에 연결할 수 없어요 ({type(e).__name__}). 컨테이너가 켜져 있는지 확인해 주세요.")
    c = Conn(raw, "postgres", lambda: pool.putconn(raw))
    if "postgres" not in _ready:
        with _ready_lock:
            try:
                _create_all(c, "BIGSERIAL PRIMARY KEY")
                for sql in POSTGRES_UPGRADES:
                    c.execute(sql)
                raw.commit()
                _grant_readonly(c)
                _ready.add("postgres")
            except Exception:
                raw.rollback()
                pool.putconn(raw)
                raise
    return c


def connect(kind=None):
    """DB 연결 (kind 를 주면 설정과 다른 쪽. 이전 스크립트가 쓴다)."""
    return _connect_postgres() if (kind or backend()) == "postgres" else _connect_sqlite()


def close_pool():
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None
    _ready.discard("postgres")


def connect_readonly():
    """!SQL 전용: 쓰기를 DB 쪽에서 막은 연결. 숨김 칸(api_keys.hash)도 읽지 못한다. 오래 걸리면 중단한다."""
    if backend() == "postgres":
        return _connect_postgres_readonly()
    with connect():  # 파일과 테이블을 먼저 준비한다
        pass
    raw = sqlite3.connect(f"file:{quote(str(Path(config.DB_PATH).resolve()))}?mode=ro", uri=True,
                          detect_types=sqlite3.PARSE_DECLTYPES)
    raw.execute("PRAGMA query_only=ON")
    deadline = time.time() + READONLY_SECONDS
    raw.set_progress_handler(lambda: time.time() > deadline, 10000)

    def guard(action, a, b, _db, _trigger):
        if action == sqlite3.SQLITE_ATTACH or (action == sqlite3.SQLITE_READ and b in HIDDEN_COLUMNS.get(a, ())):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    raw.set_authorizer(guard)
    return Conn(raw, "sqlite", raw.close)


def _connect_postgres_readonly():
    if not config.POSTGRES_RO_PASSWORD:
        raise DbError("읽기 전용 계정이 없어요. .env 의 POSTGRES_RO_PASSWORD 를 넣고 python -m scripts.migrate_to_postgres --setup-only 를 실행해 주세요.")
    try:
        import psycopg
    except ImportError:
        raise DbError("psycopg 가 없어요. 저장소 맨 위에서 pip install -r requirements.txt 를 실행해 주세요.")
    try:
        raw = psycopg.connect(**_pg_options(), user=config.POSTGRES_RO_USER, password=config.POSTGRES_RO_PASSWORD, connect_timeout=5)
    except psycopg.OperationalError as e:
        raise DbError(f"읽기 전용 계정으로 연결할 수 없어요 ({type(e).__name__}).")
    raw.execute("SET TRANSACTION READ ONLY")
    raw.execute(f"SET LOCAL statement_timeout = {READONLY_SECONDS * 1000}")
    return Conn(raw, "postgres", raw.close)
