"""앱·봇용 API 키. 서버 관리자만 이 스크립트로 발급·폐기한다 (채팅이나 웹으로는 발급하지 않는다).

- 키 원문은 발급할 때 한 번만 보여 주고, DB 에는 해시(sha256)만 저장한다.
- 키마다 사람(내부 ID)이 연결된다. 입구는 키 -> 사람으로 바꿔서 비서에 넘긴다.
- 종류: app(앱. 키 주인 한 사람) / bot(디스코드 봇 같은 서버 안 입구. 여러 사람의 메시지를 대신 전달)

사용 (assistant/ 폴더에서):
  python -m core.apikeys create owner "내 맥 앱"              # 앱 키 발급 (키는 이때 한 번만 출력됨)
  python -m core.apikeys create owner "디스코드 봇" --kind bot
  python -m core.apikeys list
  python -m core.apikeys revoke 3                             # 번호로 폐기
"""
import argparse
import hashlib
import secrets

from core import users

KINDS = ("app", "bot")
PREFIX = "lai_"  # 이 서버의 키라는 표시 (다른 키와 섞여도 알아보기 쉽게)


class ApiKeyError(Exception):
    pass


def _conn():
    return users._conn()


def _hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def create(user, name, kind="app", actor="admin-script"):
    """키를 만들고 원문을 돌려준다 (원문은 저장하지 않는다)."""
    if kind not in KINDS:
        raise ApiKeyError(f"종류는 {', '.join(KINDS)} 중 하나입니다.")
    p = users.get(user)
    if p is None:
        raise ApiKeyError(f"'{user}' 사용자가 없습니다. 먼저 python -m core.users add 로 만드세요.")
    if not p["active"]:
        raise ApiKeyError(f"'{user}'는 비활성 사용자입니다.")
    key = PREFIX + secrets.token_urlsafe(32)
    with _conn() as c:
        key_id = c.insert("INSERT INTO api_keys(user_id, name, kind, hash, created) VALUES (?,?,?,?,?)",
                          (user, name, kind, _hash(key), users._now()))
        users._log(c, actor, "key_create", user, f"#{key_id} {kind} {name}")
    return key


def verify(key):
    """키가 맞고 키와 사람이 모두 활성이면 {"id", "user", "kind", "name"}, 아니면 None."""
    if not key or not key.startswith(PREFIX):
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT k.id, k.user_id, k.kind, k.name FROM api_keys k JOIN people p ON p.id = k.user_id "
            "WHERE k.hash=? AND k.active=1 AND p.active=1", (_hash(key),)).fetchone()
        if row is None:
            return None
        c.execute("UPDATE api_keys SET last_used=? WHERE id=?", (users._now(), row[0]))
    return {"id": row[0], "user": row[1], "kind": row[2], "name": row[3]}


def revoke(key_id, actor="admin-script"):
    with _conn() as c:
        n = c.execute("UPDATE api_keys SET active=0 WHERE id=? AND active=1", (int(key_id),)).rowcount
        if n:
            users._log(c, actor, "key_revoke", None, f"#{key_id}")
    return bool(n)


def list_keys():
    with _conn() as c:
        rows = c.execute("SELECT id, user_id, name, kind, active, created, last_used FROM api_keys ORDER BY id").fetchall()
    return [{"id": r[0], "user": r[1], "name": r[2], "kind": r[3], "active": bool(r[4]),
             "created": r[5], "last_used": r[6]} for r in rows]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m core.apikeys", description="API 키 관리 (서버 관리자용)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="키 발급")
    c.add_argument("user", help="내부 ID (예: owner)")
    c.add_argument("name", help="키 이름 (예: 내 맥 앱)")
    c.add_argument("--kind", choices=KINDS, default="app")
    sub.add_parser("list", help="키 목록")
    r = sub.add_parser("revoke", help="키 폐기")
    r.add_argument("id", type=int)
    a = ap.parse_args(argv)
    users.bootstrap()
    try:
        if a.cmd == "create":
            key = create(a.user, a.name, a.kind)
            print(f"발급했습니다 ({a.kind}, {a.user}, {a.name}). 아래 키는 지금 한 번만 보입니다. 앱 설정에 붙여 넣으세요.\n{key}")
        elif a.cmd == "list":
            keys = list_keys()
            if not keys:
                print("발급된 키가 없습니다.")
            for k in keys:
                state = "사용" if k["active"] else "폐기"
                used = f"{k['last_used']:%Y-%m-%d %H:%M}" if k["last_used"] else "-"
                print(f"#{k['id']}  {k['user']}  {k['kind']}  {state}  {k['name']}  "
                      f"(발급 {k['created']:%Y-%m-%d %H:%M}, 마지막 사용 {used})")
        elif a.cmd == "revoke":
            print("폐기했습니다." if revoke(a.id) else "없거나 이미 폐기된 키입니다.")
    except ApiKeyError as e:
        raise SystemExit(f"❌ {e}")


if __name__ == "__main__":
    main()
