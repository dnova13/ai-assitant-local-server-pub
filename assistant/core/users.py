"""사용자 구조. 사람마다 서버가 정한 내부 ID 하나를 두고, 입구(디스코드, 앱 등)의 ID는 연결 표로 내부 ID에 묶는다.
메모·설정·대화는 모두 내부 ID 기준이라, 같은 사람이면 어느 입구로 들어와도 같은 자료를 본다.
권한(admin/user)도 내부 ID에 붙으므로 서버 안에서 판단한다.

관리 스크립트 (서버에서 실행, 실행한 사람을 관리자로 본다):
  python -m core.users init                         # 처음 한 번: owner(관리자) 생성, .env 의 ALLOWED_USER_IDS 연결, 기존 메모 이전
  python -m core.users list                         # 사람 목록
  python -m core.users add family1 가족 [--admin]   # 사람 추가 (기본 역할 user)
  python -m core.users link family1 discord 1234    # 입구 ID 연결
  python -m core.users unlink discord 1234          # 연결 끊기
  python -m core.users deactivate family1           # 비활성화 (데이터는 남김, 되돌릴 수 있음)
  python -m core.users activate family1             # 다시 활성화
"""
import re

from core import config
from clients import db, llm

OWNER = "owner"
ROLES = ("admin", "user")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")  # 내부 ID: 영문 소문자로 시작, 2~32자
SOURCES = ("discord", "app", "telegram")  # 연결 출처 (앱은 키가 곧 사람이라 연결 대신 apikeys 를 쓴다)


class UserError(Exception):
    pass


def _conn():
    return db.connect()


def _now():
    return llm.now().replace(microsecond=0)


def _log(c, actor, action, target=None, detail=None):
    c.execute("INSERT INTO admin_log(time, actor, action, target, detail) VALUES (?,?,?,?,?)",
              (_now(), actor, action, target, detail))


def log(actor, action, target=None, detail=None):
    with _conn() as c:
        _log(c, actor, action, target, detail)


# --- 처음 설정 ---
def bootstrap(discord_ids=None):
    """사람이 한 명도 없을 때만: owner(관리자)를 만들고 디스코드 ID를 연결하고, 그 ID로 저장된 기존 메모를 owner로 옮긴다.
    이미 사람이 있으면 아무것도 하지 않고 False. (.env 의 ALLOWED_USER_IDS 는 여기서 한 번만 쓴다)"""
    ids = [str(x) for x in (config.ALLOWED_USER_IDS if discord_ids is None else discord_ids)]
    with _conn() as c:
        if c.execute("SELECT COUNT(*) FROM people").fetchone()[0]:
            return False
        now = _now()
        c.execute("INSERT INTO people(id, name, role, active, created) VALUES (?,?,?,1,?)", (OWNER, "관리자", "admin", now))
        for did in ids:
            c.execute("INSERT INTO links(source, external_id, user_id, created) VALUES ('discord',?,?,?) ON CONFLICT DO NOTHING", (did, OWNER, now))
        moved = 0
        if ids:
            moved = c.execute(f"UPDATE notes SET user_id=? WHERE user_id IN ({','.join('?' * len(ids))})", (OWNER, *ids)).rowcount
        _log(c, "system", "bootstrap", OWNER, f"디스코드 연결 {len(ids)}개, 메모 이전 {moved}개")
    return True


# --- 조회 ---
def resolve(source, external_id):
    """입구 ID -> 내부 ID. 연결이 없거나 비활성 사용자면 None."""
    with _conn() as c:
        row = c.execute(
            "SELECT p.id FROM links l JOIN people p ON p.id = l.user_id "
            "WHERE l.source=? AND l.external_id=? AND p.active=1", (source, str(external_id))).fetchone()
    return row[0] if row else None


def external_ids(user, source):
    """내부 ID -> 그 입구의 ID 목록 (예: 일정 알림을 보낼 디스코드 ID). 비활성 사용자면 빈 목록."""
    with _conn() as c:
        rows = c.execute(
            "SELECT l.external_id FROM links l JOIN people p ON p.id = l.user_id "
            "WHERE l.user_id=? AND l.source=? AND p.active=1 ORDER BY l.created", (user, source)).fetchall()
    return [r[0] for r in rows]


def get(user):
    with _conn() as c:
        row = c.execute("SELECT id, name, role, active FROM people WHERE id=?", (user,)).fetchone()
    return None if row is None else {"id": row[0], "name": row[1], "role": row[2], "active": bool(row[3])}


def is_admin(user):
    p = get(user)
    return bool(p and p["active"] and p["role"] == "admin")


def list_people():
    with _conn() as c:
        rows = c.execute("SELECT id, name, role, active FROM people ORDER BY created, id").fetchall()
        out = []
        for uid, name, role, active in rows:
            links = c.execute("SELECT source, COUNT(*) FROM links WHERE user_id=? GROUP BY source", (uid,)).fetchall()
            out.append({"id": uid, "name": name, "role": role, "active": bool(active), "links": dict(links)})
    return out


def fmt_people(people):
    lines = []
    for p in people:
        links = ", ".join(f"{s} {n}개" for s, n in p["links"].items()) or "연결 없음"
        lines.append(f"{p['id']}  {p['name']}  {p['role']}  {'활성' if p['active'] else '비활성'}  ({links})")
    return "\n".join(lines) or "사용자가 없습니다."


# --- 변경 (actor: 누가 했는지. 스크립트는 'script') ---
def add(actor, user, name, role="user"):
    if not ID_RE.match(user or ""):
        raise UserError("내부 ID는 영문 소문자로 시작하고 영문 소문자·숫자·_ 로 2~32자여야 합니다. 예: family1")
    if role not in ROLES:
        raise UserError(f"역할은 {', '.join(ROLES)} 중 하나입니다.")
    with _conn() as c:
        if c.execute("SELECT 1 FROM people WHERE id=?", (user,)).fetchone():
            raise UserError(f"이미 있는 내부 ID입니다: {user}")
        c.execute("INSERT INTO people(id, name, role, active, created) VALUES (?,?,?,1,?)", (user, name or user, role, _now()))
        _log(c, actor, "add", user, f"이름 {name}, 역할 {role}")


def link(actor, user, source, external_id):
    if source not in SOURCES:
        raise UserError(f"입구는 {', '.join(SOURCES)} 중 하나입니다.")
    external_id = str(external_id).strip()
    if not external_id:
        raise UserError("외부 ID가 비어 있습니다.")
    with _conn() as c:
        if not c.execute("SELECT 1 FROM people WHERE id=?", (user,)).fetchone():
            raise UserError(f"그런 사용자가 없습니다: {user}")
        row = c.execute("SELECT user_id FROM links WHERE source=? AND external_id=?", (source, external_id)).fetchone()
        if row:
            raise UserError(f"이미 {row[0]}에 연결된 ID입니다.")
        c.execute("INSERT INTO links(source, external_id, user_id, created) VALUES (?,?,?,?)", (source, external_id, user, _now()))
        _log(c, actor, "link", user, source)


def unlink(actor, source, external_id):
    with _conn() as c:
        row = c.execute("SELECT user_id FROM links WHERE source=? AND external_id=?", (source, str(external_id))).fetchone()
        if not row:
            raise UserError("그런 연결이 없습니다.")
        if row[0] == actor:
            raise UserError("자기 자신의 연결은 끊을 수 없습니다.")
        c.execute("DELETE FROM links WHERE source=? AND external_id=?", (source, str(external_id)))
        _log(c, actor, "unlink", row[0], source)


def _active_admins(c):
    return c.execute("SELECT COUNT(*) FROM people WHERE role='admin' AND active=1").fetchone()[0]


def deactivate(actor, user):
    """비활성화: 더는 쓸 수 없게 하고 데이터(메모, 설정, 연결)는 남긴다. 되돌릴 수 있다."""
    with _conn() as c:
        row = c.execute("SELECT role, active FROM people WHERE id=?", (user,)).fetchone()
        if not row:
            raise UserError(f"그런 사용자가 없습니다: {user}")
        if user == actor:
            raise UserError("자기 자신은 비활성화할 수 없습니다.")
        if not row[1]:
            raise UserError(f"이미 비활성 상태입니다: {user}")
        if row[0] == "admin" and _active_admins(c) <= 1:
            raise UserError("마지막 관리자는 비활성화할 수 없습니다.")
        c.execute("UPDATE people SET active=0 WHERE id=?", (user,))
        _log(c, actor, "deactivate", user)


def activate(actor, user):
    with _conn() as c:
        if not c.execute("SELECT 1 FROM people WHERE id=?", (user,)).fetchone():
            raise UserError(f"그런 사용자가 없습니다: {user}")
        c.execute("UPDATE people SET active=1 WHERE id=?", (user,))
        _log(c, actor, "activate", user)


# --- 사용자 설정 (기본 위치 등. 값은 글자 그대로 저장) ---
def get_setting(user, key):
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE user_id=? AND key=?", (user, key)).fetchone()
    return row[0] if row else None


def set_setting(user, key, value):
    with _conn() as c:
        c.execute("INSERT INTO settings(user_id, key, value, updated) VALUES (?,?,?,?) "
                  "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                  (user, key, value, _now()))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="사용자 관리 (서버 관리자용)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="처음 설정: owner 생성, ALLOWED_USER_IDS 연결, 기존 메모 이전")
    sub.add_parser("list", help="사람 목록")
    p = sub.add_parser("add", help="사람 추가")
    p.add_argument("user"); p.add_argument("name"); p.add_argument("--admin", action="store_true")
    p = sub.add_parser("link", help="입구 ID 연결")
    p.add_argument("user"); p.add_argument("source", choices=SOURCES); p.add_argument("external_id")
    p = sub.add_parser("unlink", help="연결 끊기")
    p.add_argument("source", choices=SOURCES); p.add_argument("external_id")
    p = sub.add_parser("deactivate", help="비활성화"); p.add_argument("user")
    p = sub.add_parser("activate", help="다시 활성화"); p.add_argument("user")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "init":
            print("owner(관리자)를 만들고 설정했습니다." if bootstrap() else "이미 설정되어 있습니다 (사람이 있음).")
        elif a.cmd == "add":
            add("script", a.user, a.name, "admin" if a.admin else "user"); print(f"추가했습니다: {a.user}")
        elif a.cmd == "link":
            link("script", a.user, a.source, a.external_id); print("연결했습니다.")
        elif a.cmd == "unlink":
            unlink("script", a.source, a.external_id); print("연결을 끊었습니다.")
        elif a.cmd == "deactivate":
            deactivate("script", a.user); print(f"비활성화했습니다: {a.user}")
        elif a.cmd == "activate":
            activate("script", a.user); print(f"활성화했습니다: {a.user}")
        print(fmt_people(list_people()))
    except UserError as e:
        raise SystemExit(f"❌ {e}")


if __name__ == "__main__":
    main()
