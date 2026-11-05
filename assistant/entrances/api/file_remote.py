"""비서 봇의 `!파일`: 말을 앱으로 전달하고 답을 돌려준다. 정형 명령은 앱이, 자연어는 앱에 연결된 비서가 해석한다.

- 허용 폴더 검사, 인증 코드 검증, 실행은 전부 앱이 한다. 서버는 어느 컴퓨터로 보낼지만 고른다.
- 인증 코드는 저장하지 않는다 (DB, 로그, 대화 기록 모두). 앱이 "기다리는 중"이면 다음 말을 답으로 넘긴다.
"""
import asyncio
import base64
import re
import secrets
import time
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool

from features import app_computers

VIA = "비서 봇"
TURN_SECONDS = 120  # 큰 파일 복사까지 기다린다
ANSWER_SECONDS = 60  # 앱의 확인 시간과 같다
MAX_REPLY = 8000
CODE = re.compile(r"^!?인증\s*\d{3}\s?\d{3}$")
BARE_CODE = re.compile(r"^!?\d{3}\s?\d{3}$")
AUTH_COMMAND = re.compile(r"^!?인증(?:\s|$)")
CONTROL = ("인증", "복구", "잠금", "상태")  # 파일 명령이 아니라 앱의 원격 규칙으로 가는 말
HELP = ("파일 명령 (앱을 켜 둔 내 컴퓨터의 허용 폴더 안에서만)\n"
        "  !파일 이동 <이름> <폴더>       기본 컴퓨터에서\n"
        "  !파일 소니 이동 <이름> <폴더>  소니에서\n"
        "  !파일 도움말                   앱이 아는 전체 명령\n"
        "  !디스코드 채널                 채널 등록·목록·해제 도움말\n"
        "  인증 <6자리 코드>              기본 컴퓨터 잠금 풀기 (DM 에서만)\n"
        "  !파일 소니 인증 <6자리 코드>   소니 잠금 풀기 (DM 에서만)\n")
CHANNEL_HELP = (
    "- 디스코드 채널 사용법\n"
    "  !디스코드 채널 / !디스코드 채널 도움말\n"
    "  !디스코드 채널 지정 [채널 ID]\n"
    "  !디스코드 채널 등록 [채널 ID] — 지정과 같음\n"
    "  !디스코드 채널 목록\n"
    "  !디스코드 채널 해체 [채널 ID]\n"
    "  !디스코드 채널 삭제 [채널 ID] — 해체와 같음\n\n"
    "- 적용 위치\n\n"
    "  DM에서는 채널 ID를 입력하세요.\n"
    "  채널 안에서 ID를 생략하면 현재 채널에 적용합니다.\n"
    "  디스코드 채널 …도 같습니다. 파일 명령은 파일 / !파일을 그대로 씁니다.\n\n"
    "- 안내\n\n"
    "  지정 채널의 파일 요청은 먼저 인증해야 합니다. 인증 코드는 봇 DM에서만 보내세요.\n"
    "  해체·삭제는 등록만 해제하며 디스코드 채널 자체를 지우지 않습니다."
)
NO_COMPUTER = "등록한 앱 컴퓨터가 없어요. 앱 설정 > 원격 > 비서 서버 연결에서 등록해 주세요."

MAX_FILE_BYTES = 20 * 1024 * 1024
FILE_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")  # 받기: 디스코드 첨부 주소만

_turns = {}  # 요청 번호 -> (컴퓨터 번호, Future)
_files = {}  # 요청 번호 -> 앱이 올린 파일. 답과 함께 봇으로 넘기고 바로 버린다 (저장하지 않는다)
_waiting = {}  # 사람 -> {"computer", "until", "place"}  앱이 답을 기다리는 중


def on_reply(computer_id, message):
    """앱에서 온 답. 그 컴퓨터로 보낸 요청의 답만 받는다."""
    turn = _turns.get(message.get("id"))
    if turn and turn[0] == computer_id and not turn[1].done():
        turn[1].set_result(message)


def attach(computer_id, turn_id, name, data, note, days):
    """앱이 올린 파일을 그 요청의 답에 붙인다. 지금 진행 중인 그 컴퓨터의 요청이 아니면 False."""
    turn = _turns.get(turn_id)
    if not turn or turn[0] != computer_id:
        return False
    _files[turn_id] = {"name": name, "data": data, "note": note, "days": days}
    return True


def clean_files(files):
    """받기: 형식이 맞고 디스코드 주소인 첨부만."""
    out = []
    for f in files if isinstance(files, list) else []:
        if not isinstance(f, dict) or not isinstance(f.get("url"), str):
            continue
        parts = urlsplit(f["url"])
        if parts.scheme == "https" and parts.hostname in FILE_HOSTS:
            out.append({"id": str(f.get("id") or ""), "name": str(f.get("name") or "file")[:200],
                        "size": int(f.get("size") or 0), "url": f["url"]})
    return out


def on_disconnect(computer_id):
    for owner, future in _turns.values():
        if owner == computer_id and not future.done():
            future.set_result(None)
    for user, wait in list(_waiting.items()):
        if wait["computer"]["id"] == computer_id:
            del _waiting[user]


async def _turn(computer, kind, text, files=None, *, channel=False, prompt_id=None):
    """앱으로 말을 보내고 답을 기다린다. 앱이 답하지 않으면 None."""
    link = app_computers.link_of(computer["id"])
    if link is None:
        return None
    turn_id = "t_" + secrets.token_urlsafe(9)
    future = asyncio.get_running_loop().create_future()
    _turns[turn_id] = (computer["id"], future)
    try:
        message = {"type": kind, "id": turn_id, "text": text, "via": VIA}
        if prompt_id:
            message["promptId"] = prompt_id
        if channel:
            message["channel"] = True
        if files:
            message["files"] = files
        await link.send_json(message)
        answer = await asyncio.wait_for(future, TURN_SECONDS)
        if answer is not None and turn_id in _files:
            return {**answer, "file": _files[turn_id]}
        return answer
    except (asyncio.TimeoutError, RuntimeError):
        return None
    finally:
        del _turns[turn_id]
        _files.pop(turn_id, None)


def clean_prompt(raw):
    """버튼은 글자를 재해석하지 않고 앱이 보낸 질문 번호에 연결한다."""
    if not isinstance(raw, dict):
        return None
    pid, kind, until = raw.get("id"), raw.get("kind"), raw.get("expiresAt")
    if (not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pid)
            or kind not in ("confirm", "choice") or not isinstance(until, (int, float))
            or not time.time() * 1000 < until <= (time.time() + ANSWER_SECONDS + 2) * 1000):
        return None
    out = {"id": pid, "kind": kind, "expiresAt": until}
    if kind == "confirm":
        out["destructive"] = raw.get("destructive") is True
    else:
        options = raw.get("options")
        if not isinstance(options, list) or not 1 <= len(options) <= 20 or any(not isinstance(o, str) or len(o) > 500 for o in options):
            return None
        out["options"] = options
    return out


def _out(user, computer, place, answer):
    if answer is None:
        return {"user": user, "reply": f"{computer['name']} 앱이 답하지 않아요. 앱이 켜져 있는지 확인해 주세요."}
    text = str(answer.get("text") or "")[:MAX_REPLY] or "(답이 없습니다)"
    out = {"user": user, "reply": text}
    if answer.get("file"):
        f = answer["file"]
        out["file"] = {"name": f["name"], "note": f["note"], "days": f["days"], "data": base64.b64encode(f["data"]).decode("ascii")}
    if answer.get("waiting") is True:
        prompt = clean_prompt(answer.get("prompt"))
        _waiting[user] = {"computer": computer, "until": min(time.time() + ANSWER_SECONDS, prompt["expiresAt"] / 1000) if prompt else time.time() + ANSWER_SECONDS, "place": place, "prompt": prompt}
        out["file_waiting"] = True
        if prompt:
            out["file_prompt"] = prompt
    else:
        _waiting.pop(user, None)
    return out


def _command_text(rest):
    """앱으로 보낼 글자. 인증·잠금은 그대로, 나머지는 같은 파일 해석 경로로 보낸다."""
    words = rest.split()
    if not words:
        return "!파일 도움말"
    if words[0] in CONTROL or words[:2] == ["원격", "중지"] or BARE_CODE.match(rest):
        return rest
    return f"!파일 {rest}"


async def _answer(user, text, dm, place):
    """앱이 기다리던 답을 넘긴다. 기다리는 중이 아니거나 다른 명령이면 None."""
    wait = _waiting.get(user)
    if not wait:
        return None
    if time.time() > wait["until"] or wait["place"] != place:
        _waiting.pop(user, None)
        return None
    word = text[1:].strip() if text.startswith("!") and text[1:].strip()[:1].isdigit() else text
    if re.fullmatch(r"!?인증\s+폐기", word):
        _waiting.pop(user, None)
        return None  # 확인 답변으로 소비하지 않고 앱의 잠금 명령으로 전달한다
    if word.startswith("!") and not re.match(r"^!파일(\s|$)", word):  # 다른 명령: 앱의 질문을 취소하고 평소처럼 처리한다
        _waiting.pop(user, None)
        await _turn(wait["computer"], "answer", "취소", channel=not dm)
        return None
    if re.match(r"^!파일(\s|$)", word):
        return None
    if not dm and (CODE.match(word) or BARE_CODE.match(word)):
        return {"user": user, "reply": "인증 코드는 DM 으로만 보내 주세요. 방금 메시지는 지웠어요.", "delete_source": True}
    _waiting.pop(user, None)
    return _out(user, wait["computer"], place, await _turn(wait["computer"], "answer", word, channel=not dm, prompt_id=(wait.get("prompt") or {}).get("id")))


async def _channel_command(user, words, dm, current_id=None, current_name=""):
    """DM은 명시 ID, 채널에서는 ID를 생략하면 현재 채널을 관리한다."""
    if not words or words == ["도움말"]:
        return CHANNEL_HELP
    action = words[0]
    if words == ["목록"]:
        rows = await run_in_threadpool(app_computers.channel_entries, user)
        lines = [f"- {r['name']} ({r['id']})" if r["name"] != r["id"] else f"- {r['id']}" for r in rows]
        return "지정한 파일 채널:\n" + ("\n".join(lines) if lines else "없음")
    if action not in ("지정", "등록", "해체", "삭제") or len(words) not in (1, 2):
        return CHANNEL_HELP
    if len(words) == 1 and dm:
        return "DM에서는 채널 ID를 입력해 주세요.\n" + CHANNEL_HELP
    channel_id = str(words[1] if len(words) == 2 else current_id or "")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", channel_id) or int(channel_id) > 2**64 - 1:
        return "채널 ID는 디스코드에서 복사한 숫자로 입력해 주세요.\n" + CHANNEL_HELP
    if action in ("해체", "삭제"):
        reply = await run_in_threadpool(app_computers.release, user, channel_id)
        wait = _waiting.get(user)
        if wait and wait["place"] == channel_id:
            _waiting.pop(user, None)  # 해제한 채널의 이전 확인 답도 앱에 전달하지 않는다.
        return reply
    try:
        return await run_in_threadpool(app_computers.designate, user, channel_id, current_name if not dm and channel_id == str(current_id) and current_name else channel_id)
    except app_computers.ComputerError as e:
        return str(e)


async def _receive(user, text, files):
    """비서 봇 DM 의 첨부를 앱에 알린다. 내려받기는 앱이 디스코드에서 직접 한다."""
    computers = await run_in_threadpool(app_computers.list_for, user)
    if not computers:
        return {"user": user, "reply": None if not text else NO_COMPUTER}
    name = text[len("!파일"):].strip()
    computer = await run_in_threadpool(app_computers.by_name, user, name) if name else None
    if name and computer is None:
        return None  # 첨부만 보내는 말이 아니다: !파일 이동 … 은 명령으로 처리한다
    computer = computer or await run_in_threadpool(app_computers.default_of, user)
    if app_computers.link_of(computer["id"]) is None:
        return {"user": user, "reply": f"{computer['name']} 앱이 꺼져 있어 파일을 받을 수 없어요. 앱을 켠 뒤 다시 보내 주세요."}
    return _out(user, computer, None, await _turn(computer, "command", "", files))


async def handle(user, text, dm, channel_id=None, channel_name="", files=None, prompt_id=None):
    """파일 쪽 말이면 답(dict), 아니면 None (평소처럼 비서가 처리한다)."""
    text = re.sub(r"^(파일|디스코드)(?=\s|$)", r"!\1", (text or "").strip())
    words = text.split()
    place = None if dm else channel_id
    if prompt_id is not None:
        wait = _waiting.get(user)
        allowed_place = dm or await run_in_threadpool(app_computers.is_designated, user, channel_id)
        if (not wait or wait["place"] != place or wait["until"] <= time.time()
                or (wait.get("prompt") or {}).get("id") != prompt_id or not allowed_place):
            out = {"user": user, "reply": "이미 끝났거나 사용할 수 없는 질문입니다. 현재 질문에 답해 주세요."}
            if wait and wait["place"] == place and wait["until"] > time.time() and allowed_place:
                out["file_waiting"] = True
                if wait.get("prompt"):
                    out["file_prompt"] = wait["prompt"]
            return out
    if words[:1] == ["!디스코드"] or words[:2] == ["!파일", "채널"]:
        if words[:1] == ["!디스코드"]:
            reply = await _channel_command(user, words[2:], dm, channel_id, channel_name) if words[1:2] == ["채널"] else CHANNEL_HELP
        else:
            reply = "채널 관리는 !디스코드 채널로 옮겼습니다.\n" + CHANNEL_HELP
        out = {"user": user, "reply": reply}
        wait = _waiting.get(user)
        if wait and wait["place"] == place and wait["until"] > time.time():
            out["file_waiting"] = True
            if wait.get("prompt"):
                out["file_prompt"] = wait["prompt"]
        return out
    if dm and files and (not text or re.match(r"^!파일(\s|$)", text)):
        files = clean_files(files)
        if files:
            out = await _receive(user, text, files)
            if out is not None:
                return out
        elif not text:
            return {"user": user, "reply": None}
    out = await _answer(user, text, dm, place)
    if out is not None:
        return out
    is_code = bool(CODE.match(text))
    is_auth = bool(AUTH_COMMAND.match(text))
    if not is_auth and not re.match(r"^!파일(\s|$)", text):
        return None
    computers = await run_in_threadpool(app_computers.list_for, user)
    if not computers:
        if is_code and dm:
            return None
        return {"user": user, "reply": NO_COMPUTER if dm else None}  # 채널의 남에게는 반응하지 않는다
    if not dm and not await run_in_threadpool(app_computers.is_designated, user, channel_id):
        return {"user": user, "reply": None if is_code else "이 채널은 지정되지 않았어요. 이 채널에서 `!디스코드 채널 지정`을 보내거나 봇 DM에서 `!디스코드 채널 지정 <채널 ID>`로 등록하세요."}
    if is_auth and not dm:
        return {"user": user, "reply": "인증 코드는 DM 으로만 보내 주세요. 방금 메시지는 지웠어요.", "delete_source": True}
    if not dm and re.match(r"^!파일\s+(?:\S+\s+)?(?:인증|복구)(?:\s|$)", text):
        return {"user": user, "reply": "인증 코드는 DM 으로만 보내 주세요. 방금 메시지는 지웠어요.", "delete_source": True}
    if is_auth:
        computer, rest = await run_in_threadpool(app_computers.default_of, user), text.lstrip("!")
    else:
        rest = text[len("!파일"):].strip()
        if not rest:
            lines = [f"- {c['name']}: {app_computers.status_word(c['id'])}" for c in computers]
            return {"user": user, "reply": HELP + "\n".join(["컴퓨터", *lines])}
        first = rest.split()[0]
        computer = await run_in_threadpool(app_computers.by_name, user, first)
        if computer:
            rest = rest[len(first):].strip()
        else:
            computer = await run_in_threadpool(app_computers.default_of, user)
    if app_computers.link_of(computer["id"]) is None:
        return {"user": user, "reply": f"{computer['name']} 앱이 꺼져 있거나 서버 원격 허용이 꺼져 있어요."}
    return _out(user, computer, place, await _turn(computer, "command", _command_text(rest), channel=not dm))
