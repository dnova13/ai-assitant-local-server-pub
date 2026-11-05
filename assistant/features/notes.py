"""메모 저장소 (clients/db.py). 사용자별로 분리된다."""
from datetime import timedelta

from clients import db, llm


def add(user, text):
    with db.connect() as c:
        return c.insert("INSERT INTO notes(user_id, text, created) VALUES (?,?,?)",
                        (user, text, llm.now().replace(microsecond=0)))


def recent(user, limit=10):
    with db.connect() as c:
        return c.execute(
            "SELECT id, text, created FROM notes WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user, limit),
        ).fetchall()


def get(user, note_id):
    """번호로 메모 한 개 (없으면 None). 다른 사용자의 메모는 보이지 않는다."""
    with db.connect() as c:
        return c.execute("SELECT id, text, created FROM notes WHERE user_id=? AND id=?", (user, note_id)).fetchone()


def search(user, keyword, limit=20):
    with db.connect() as c:
        return c.execute(
            "SELECT id, text, created FROM notes WHERE user_id=? AND LOWER(text) LIKE LOWER(?) "
            "ORDER BY id DESC LIMIT ?",
            (user, f"%{keyword}%", limit),
        ).fetchall()


def since(user, days):
    with db.connect() as c:
        return c.execute(
            "SELECT id, text, created FROM notes WHERE user_id=? AND created>=? ORDER BY id",
            (user, llm.now() - timedelta(days=days)),
        ).fetchall()


def delete(user, note_id):
    with db.connect() as c:
        return c.execute("DELETE FROM notes WHERE user_id=? AND id=?", (user, note_id)).rowcount


def everything(user):
    with db.connect() as c:
        return c.execute("SELECT id, text, created FROM notes WHERE user_id=? ORDER BY id", (user,)).fetchall()


def delete_many(user, ids):
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    with db.connect() as c:
        return c.execute(f"DELETE FROM notes WHERE user_id=? AND id IN ({','.join('?' * len(ids))})", [user, *ids]).rowcount


def fmt(rows):
    return "\n".join(f"#{nid} [{db.local(created):%m-%d %H:%M}] {text}" for nid, text, created in rows)
