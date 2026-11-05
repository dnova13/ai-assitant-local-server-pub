"""SQLite 비서 DB 를 Postgres 로 옮긴다. 읽기 전용 계정(!SQL 용)도 여기서 만든다.
실행 (assistant/ 폴더, 컨테이너를 켠 뒤, 서비스 두 개를 멈추고):
  python -m scripts.migrate_to_postgres                # 옮기기
  python -m scripts.migrate_to_postgres --setup-only   # 테이블과 읽기 전용 계정만 (비밀번호를 바꿀 때도)
- 원본 SQLite 파일은 바꾸지 않는다 (복사본에서 시각 형식을 고쳐 옮긴다). 읽지 못하는 시각이 있으면 아무것도 옮기지 않고 멈춘다.
- 대상에 자료가 있으면 멈춘다. 한 트랜잭션이라 실패하면 아무것도 남지 않는다."""
import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from clients import db
from core import config


class MigrateError(Exception):
    pass


def setup_readonly(c):
    """읽기 전용 계정: 쓰기 불가, 숨김 칸 제외, 오래 걸리면 중단. 있으면 비밀번호와 권한만 다시 맞춘다."""
    from psycopg import sql
    if not config.POSTGRES_RO_PASSWORD:
        raise MigrateError("POSTGRES_RO_PASSWORD 가 없어요. .env 에 넣고 다시 실행해 주세요.")
    role = sql.Identifier(config.POSTGRES_RO_USER)
    password = sql.Literal(config.POSTGRES_RO_PASSWORD)
    exists = c.execute("SELECT 1 FROM pg_roles WHERE rolname=?", (config.POSTGRES_RO_USER,)).fetchone()
    verb = sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}") if exists else sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}")
    c.raw.execute(verb.format(role, password))
    c.raw.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role))
    c.raw.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(role, sql.Literal(f"{db.READONLY_SECONDS}s")))
    c.raw.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(config.POSTGRES_DB), role))
    c.raw.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    for table in db.TABLES:
        c.raw.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.Identifier(table), role))
        columns = sql.SQL(", ").join(sql.Identifier(n) for n in db.readonly_columns(c, table))
        c.raw.execute(sql.SQL("GRANT SELECT ({}) ON {} TO {}").format(columns, sql.Identifier(table), role))


def _next_ids(source, table):
    """다음 번호의 기준: 지운 번호를 다시 쓰지 않도록 SQLite 의 번호 카운터를 본다."""
    seq = source.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
    top = source.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]
    return max(seq[0] if seq else 0, top or 0)


def copy_all(source, target):
    """{테이블: 옮긴 행 수}. 끝나고 행 수를 비교한다."""
    for table in db.TABLES:
        if target.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            raise MigrateError(f"대상의 {table} 에 이미 자료가 있어요. 빈 DB 로만 옮깁니다.")
    counts = {}
    for table in db.TABLES:
        names = [name for name, _ in source.columns(table)]
        rows = source.execute(f"SELECT {', '.join(names)} FROM {table}").fetchall()
        place = ", ".join("?" * len(names))
        for row in rows:
            target.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({place})", row)
        got = target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if got != len(rows):
            raise MigrateError(f"{table} 의 행 수가 달라요: 원본 {len(rows)}, 대상 {got}")
        counts[table] = got
        if table in db.ID_TABLES:
            top = _next_ids(source, table)
            target.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), ?, ?)", (max(top, 1), top > 0))
    return counts


def migrate(sqlite_path):
    """복사본을 열어 옮긴다 (복사본에서 옛 형식을 고친다). {테이블: 행 수}."""
    if not Path(sqlite_path).exists():
        raise MigrateError(f"SQLite 파일이 없어요: {sqlite_path}")
    real_path = config.DB_PATH
    with tempfile.TemporaryDirectory() as tmp:
        config.DB_PATH = str(Path(tmp) / "copy.db")
        shutil.copy2(sqlite_path, config.DB_PATH)
        try:
            with db.connect("sqlite") as source, db.connect("postgres") as target:
                setup_readonly(target)
                return copy_all(source, target)
        finally:
            config.DB_PATH = real_path


def main():
    ap = argparse.ArgumentParser(description="SQLite 비서 DB 를 Postgres 로 옮긴다")
    ap.add_argument("--sqlite", default=config.DB_PATH, help="원본 파일 (기본: 지금 설정의 파일)")
    ap.add_argument("--setup-only", action="store_true", help="테이블과 읽기 전용 계정만 만든다")
    a = ap.parse_args()
    try:
        if a.setup_only:
            with db.connect("postgres") as c:
                setup_readonly(c)
            print("테이블과 읽기 전용 계정을 준비했어요.")
            return
        counts = migrate(a.sqlite)
        print("옮겼어요. 행 수가 원본과 같습니다.")
        for table, n in counts.items():
            print(f"- {table}: {n}행")
        print("이제 .env 를 DB_BACKEND=postgres 로 두고 서비스를 켜 주세요.")
    except (MigrateError, db.DbError) as e:
        sys.exit(f"멈췄어요 (옮긴 것 없음): {e}")
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
