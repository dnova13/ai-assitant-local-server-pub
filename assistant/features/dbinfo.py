"""DB 보기: 상태 한 줄, 테이블 목록·구조, 최근 행, 읽기 전용 SQL (README.md). 쓰기 기능은 없다.
- 관리자만 쓴다 (core/assistant.py). 행 내용이 보이는 기능은 DM 과 등록한 서버·채널에서만 쓴다.
- 모델을 쓰지 않는다. 명령어(`!DB`, `!SQL`)로만 받고, 테이블 이름은 코드가 고정 목록으로 바꾼다.
- 임의 SQL 은 쓰기가 막힌 연결로만 실행한다 (clients/db.py). 아래 검사는 안내용 1차 거르기이고 막는 것은 DB 권한이다."""
import re
from datetime import datetime
from pathlib import Path

from clients import db
from core import config

DEFAULT_ROWS = 10
MAX_VIEW_ROWS = 100  # !DB <테이블>
MAX_SQL_ROWS = 100  # !SQL
MAX_CELL = 60
MAX_OUT = 12000  # 디스코드에서는 여러 메시지로 나눠 보낸다 (discord_rules.split_message)
ORDER_BY = {"app_computers": "created DESC, name", "app_file_channels": "created DESC", "app_sent_files": "delete_at", "managed_channels": "created DESC", "channel_messages": "at DESC", "news_categories": "created DESC",
            "news_excluded": "created DESC", "news_sent": "sent DESC", "people": "created DESC, id", "links": "created DESC", "settings": "updated DESC", "computers": "created DESC, name"}  # 나머지는 번호 역순
ALIASES = {"일정": "events", "알림": "events", "메모": "notes", "사용자": "people", "사람": "people", "연결": "links",
           "링크": "links", "설정": "settings", "키": "api_keys", "로그": "admin_log", "기록": "admin_log",
           "컴퓨터": "computers"}
READ_WORDS = ("select", "with", "explain", "show")
ROWS_PLACE_ONLY = "행 내용은 DM 이나 등록한 서버·채널에서만 볼 수 있어요."
COLUMN_WORDS = ("컬럼", "칼럼", "칸", "구조", "column")
USAGE = "사용법: DB 테이블 / DB <테이블> [개수] / DB <테이블> 컬럼  (예: DB people 컬럼)"


def find_table(word):
    """테이블 이름이나 별칭 -> 테이블 이름. 모르는 이름이면 None (쿼리에는 이 결과만 넣는다)."""
    word = (word or "").strip().lower()
    return word if word in db.TABLES else ALIASES.get(word)


def _unknown(word):
    return f"'{word}' 테이블은 없어요. 테이블: {', '.join(db.TABLES)}"


# --- 상태 ---
def _size(n):
    return f"{n / 1024 ** 2:.1f}MB" if n >= 1024 ** 2 else f"{n // 1024}KB"


def status_line():
    """!상태 에 넣는 한 줄. 어떤 오류든 상태 전체를 막지 않는다."""
    try:
        with db.connect() as c:
            if c.kind == "postgres":
                size, count = c.execute("SELECT pg_database_size(current_database()), (SELECT COUNT(*) FROM pg_stat_activity "
                                        "WHERE datname = current_database())").fetchone()
                return f"- DB: PostgreSQL 정상, 크기 {_size(size)}, 연결 {count}개"
        return f"- DB: SQLite 정상, 크기 {_size(Path(config.DB_PATH).stat().st_size)}"
    except Exception as e:
        return f"- DB: 연결 안 됨 ({type(e).__name__})"


# --- 테이블 ---
def _count(c, table):
    return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def table_list():
    with db.connect() as c:
        lines = [f"- {table}: {_count(c, table)}행" for table in db.TABLES]
    name = "PostgreSQL" if db.backend() == "postgres" else "SQLite"
    return "\n".join([f"DB 테이블 {len(lines)}개 ({name})", *lines])


def table_info(word, show_rows=True):
    """칸 이름과 형식, 복사해서 쓰는 칸 이름 줄. show_rows 이면 바로 실행할 조회문도 붙인다 (조회 안 되는 칸은 뺀다)."""
    table = find_table(word)
    if table is None:
        return _unknown(word)
    with db.connect() as c:
        columns, count = c.columns(table), _count(c, table)
    hidden = db.HIDDEN_COLUMNS.get(table, ())
    lines = [f"- {name}: {kind}{' (조회 안 됨)' if name in hidden else ''}" for name, kind in columns]
    names = ", ".join(name for name, _ in columns if name not in hidden)
    blocks = ["칸 이름:", f"```text\n{names}\n```"]
    if show_rows:
        blocks += ["바로 쓰는 조회:", f"```sql\n!SQL SELECT {names} FROM {table} LIMIT {DEFAULT_ROWS}\n```"]
    return "\n".join([f"{table}: {count}행", *lines, *blocks])


def _cell(value):
    if value is None:
        return "-"
    text = f"{db.local(value):%Y-%m-%d %H:%M:%S}" if isinstance(value, datetime) else str(value)
    text = " ".join(text.split())
    return text if len(text) <= MAX_CELL else text[:MAX_CELL] + "…"


def _rows_text(head, names, rows):
    body = [" | ".join(names), *(" | ".join(_cell(v) for v in row) for row in rows)]
    text = "\n".join(body).replace("```", "'''")
    if len(text) > MAX_OUT:
        text = text[:MAX_OUT] + "\n…(생략)"
    return f"{head}\n```\n{text}\n```"


def recent_rows(word, limit=DEFAULT_ROWS):
    table = find_table(word)
    if table is None:
        return _unknown(word)
    asked = int(limit)
    limit = max(1, min(asked, MAX_VIEW_ROWS))
    order = ORDER_BY.get(table, "id DESC")
    with db.connect() as c:
        names = [n for n, _ in c.columns(table) if n not in db.HIDDEN_COLUMNS.get(table, ())]
        rows = c.execute(f"SELECT {', '.join(names)} FROM {table} ORDER BY {order} LIMIT ?", (limit,)).fetchall()
    if not rows:
        return f"{table}: 행이 없어요."
    cap = f" (한 번에 {MAX_VIEW_ROWS}행까지 보여요)" if asked > MAX_VIEW_ROWS else ""
    return _rows_text(f"{table} 최근 {len(rows)}행{cap}", names, rows)


def command(rest, show_rows):
    """!DB 뒤의 글자를 코드가 읽는다. 테이블 이름 + 컬럼 말이면 구조, 그 밖에는 행. 뒤에 붙은 말은 개수만 읽는다.
    예: DB 테이블 / DB people / DB people 100 / DB people 데이터 100개만 보여줘 / DB people 컬럼 알려줘 / DB 컬럼 people"""
    words = rest.split()
    if not words or words in (["테이블"], ["목록"], ["테이블", "목록"]):
        return table_list()
    name = next((w for w in words if find_table(w)), None)
    columns = any(w in rest.lower() for w in COLUMN_WORDS)
    keyword = words[0] in ("테이블", "보기") or columns
    if name is None:
        others = [w for w in words if w not in ("테이블", "보기") and not any(c in w.lower() for c in COLUMN_WORDS)]
        return _unknown(others[0]) if keyword and others else USAGE
    if columns or (words[0] == "테이블" and "데이터" not in rest):
        return table_info(name, show_rows)
    if not show_rows:
        return ROWS_PLACE_ONLY
    others = " ".join(w for w in words if w != name)
    number = re.search(r"(?<!\S)\d+(?!\S)", others)
    return recent_rows(name, limit_of(others, None) or (number.group(0) if number else DEFAULT_ROWS))


# --- 읽기 전용 SQL ---
def clean_sql(text):
    """(SQL, 안내). 한 문장의 조회만 통과시킨다. 코드 블록과 끝의 ; 는 뗀다."""
    sql = text.strip()
    fence = re.fullmatch(r"```(?:sql)?\s*(.*?)\s*```", sql, re.S | re.I)
    if fence:
        sql = fence.group(1)
    sql = sql.strip().rstrip(";").strip()
    word = re.match(r"[A-Za-z]*", sql).group(0).lower()
    if word not in READ_WORDS:
        return None, "조회만 할 수 있어요 (SELECT, WITH, EXPLAIN, SHOW)."
    if re.search(r"\$\w*\$|--|/\*", sql):
        return None, "주석과 $$ 문자열은 쓸 수 없어요."
    quote = None
    for ch in sql:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == ";":
            return None, "한 번에 한 문장만 실행해요."
    return sql, None


def _sql_hint(message):
    """DB 오류에 붙일 안내. 키 칸 권한 오류와 없는 칸 오류만."""
    low = message.lower()
    if "api_keys" in low and ("permission denied" in low or "prohibited" in low):
        return "\napi_keys 의 hash 칸은 볼 수 없어요. 칸 이름을 적어 주세요 (SELECT * 안 됨)."
    if "no such column" in low or ("column" in low and "does not exist" in low):
        return "\n칸 이름은 `!DB <테이블> 컬럼`으로 볼 수 있어요."
    return ""


def run_sql(text):
    sql, problem = clean_sql(text)
    if problem:
        return problem
    try:
        with db.connect_readonly() as c:
            cur = c.raw.execute(sql)  # 사용자가 쓴 글자 그대로 (자리표시자 바꾸기를 거치지 않는다)
            names = [d[0] for d in cur.description or ()]
            rows = cur.fetchmany(MAX_SQL_ROWS + 1) if names else []
    except db.DbError:
        raise
    except Exception as e:  # DB 가 돌려준 오류 (문법, 권한, 시간 초과)
        message = (str(e).strip().splitlines() or [type(e).__name__])[0]
        return f"⚠️ 실행 실패: {message[:300]}{_sql_hint(message)}"
    if not names:
        return "결과가 없어요."
    head = f"결과 {MAX_SQL_ROWS}행까지만 보여요." if len(rows) > MAX_SQL_ROWS else f"결과 {len(rows)}행"
    return _rows_text(head, names, rows[:MAX_SQL_ROWS])


def limit_of(text, default=DEFAULT_ROWS):
    """문장의 개수 ('최근 3개'). 없으면 기본값."""
    m = re.search(r"(\d+)\s*(?:개|건|행|줄)", text)
    return int(m.group(1)) if m else default
