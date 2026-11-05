"""비서 핵심 로직.
- `!` 명령어: 모델 없이 코드가 바로 실행한다.
- 그 밖의 자유 문장: 분류기(모델)가 뜻(의도, 지역 글자, 날짜 글자)만 읽고, 판정·값 조회·실행은 코드가 한다.
- 서버 관리 명령은 분류기에서 제외한다 (명령어로만).
- user 는 내부 ID(users.py)다. 입구(디스코드 등)가 바꿔서 넘긴다.
- 파일 문장은 앱(/v1)에서만 신호(client_action)로 바꾼다. 다른 입구에서는 앱에서 하라고 안내한다 (features/files.py).
- 문장 맨 앞이 "문서"이면 문서 검색을 먼저 해 보고, 못 찾으면 안내 줄을 붙여 평소처럼 처리한다 (features/documents.py).
  자유 문장 순서: 파일 단서 -> 맨 앞 "문서" -> 평소 분류기 (README.md).
- 서버·DB의 실제 상태 조회는 명령어로만 받는다. 자유 문장으로 받지 않는다 (README.md).
- show_rows: 행 내용이 보이는 DB 기능을 써도 되는 곳인가 (디스코드는 DM 과 등록한 서버·채널만 True)."""
import re
import threading

import time

from core import classifier, config, users
from clients import db, llm
from features import app_computers, briefing, channel_manage, dbinfo, documents, files, news, notes, schedule, sysadmin, weather

HELP = """사용법 (명령어는 앞에 ! 또는 / 를 붙여요. 붙이지 않은 문장은 AI가 뜻을 읽어요. 디스코드 채널에서는 등록한 서버·채널에서만 문장을 받아요)
  <그냥 문장>      AI에게 질문. 날씨는 "부산 비 와?", 메모는 "내일 치과 3시 메모해줘",
                   "내 메모 보여줘", "3번 메모 지워줘", "이번 주 메모 정리해줘",
                   일정은 "내일 3시 치과 일정 잡아줘", "내 일정 뭐 있어?"처럼 말해도 됨
  메모 <내용>      메모 저장
  메모 목록 [개수]  최근 메모 보기 (기본 10개)
  메모 찾기 <단어>  메모 검색
  메모 삭제 <번호>  메모 삭제 (바로 지움)
  메모 <번호> 지워  메모 삭제 (묻고 나서 삭제). 문장도 됨: 3번 메모 지워줘
  메모 다 지워     메모 전체 삭제 (두 번 묻고 삭제). 문장도 됨: 메모 다 지워줘
  메모요약         최근 7일 메모를 AI가 요약
  일정 <날짜> <시각> <내용>  일정 등록 (시각이 되면 디스코드로 알림). 예: 일정 이번주 금요일 15시 치과
                   오전·오후가 없는 시각과 주 말이 없는 요일은 먼저 물어봐요
  알림 <시각> <내용>  일정과 같음. 'N분 뒤', 'N시간 후'도 됨. 예: 알림 10분 뒤 빨래
  일정 목록        앞으로의 일정 보기
  일정 삭제 <번호>  일정 삭제 (묻고 나서 삭제). 일정 <번호> 지워, 알림 <번호> 지워, 문장 "1번 일정 지워줘"도 됨
  일정 다 지워     일정 전체 삭제 (두 번 묻고 삭제). 일정 지난 거 다 지워는 지난 일정만. 문장도 됨: 약속 다 지워줘, 지난 약속 다 지워줘
  분전설정 [시점]  미리 알림 시점 보기 / 바꾸기. 예: 분전설정 30분, 20분, 10분, 5분
  브리핑           날씨·오늘 일정·최근 메모를 한 번에
  뉴스 [카테고리] [개수]  뉴스 제목과 링크. 그냥 뉴스는 it 와 일반을 5개씩. 구독, 출처 등은 뉴스 사용법
  문서 [카테고리] <질문>  내 문서(Open WebUI 지식 기반)에서 찾아 출처와 함께 답. 예: 문서 기술문서 도커 설정
  문서 카테고리    카테고리 보기
  문서 목록        색인된 문서 이름 보기
  문서 색인        문서를 올리거나 바꾼 뒤 검색에 반영 (관리자만, 바뀐 파일만)
  채널관리 지정 | 해제 | 목록   채널을 디스코드 관리 전용으로 (관리자만, 자세히: 채널관리 사용법)
  브리핑 켜기 [시각] | 브리핑 끄기   매일 아침 디스코드로 받기 (기본 07:30)
  요약 <긴 글>     붙여넣은 글을 AI가 요약
  날짜             오늘 날짜/시각
  날씨 [지역] [날짜]  지금 날씨와 3일 예보. 날짜는 오늘·내일·모레·10월 23일·23일 (지역이 없으면 기본 위치)
  기본위치 [지역]  기본 위치 보기 / 바꾸기 (지역을 말하지 않은 날씨 질문에 씀)
  상태             서버 CPU/RAM/디스크/GPU/DB
  DB 테이블        DB 테이블 목록과 행 수
  DB <테이블> [개수]  최근 행 보기 (기본 10개, 최대 100개, DM·등록한 서버와 채널에서만). 예: DB events 5
  DB <테이블> 컬럼  칸 이름과 형식 보기. 예: DB people 컬럼
  SQL <조회문>     읽기 전용 SQL (DM·등록한 서버와 채널에서만). 예: SQL SELECT id, text FROM notes LIMIT 3
  디스크 | 메모리 | 가동시간 | 도커   서버 정보 조회
  점검             서버 전체 점검 후 AI가 요약
  서비스 [목록]    볼 수 있는 서비스와 상태
  서비스 <이름>    서비스 상태 보기 (문장도 됨: 서비스 nginx 상태 알려줘)
  컴퓨터 [목록]    이 서버와 등록한 컴퓨터의 상태
  컴퓨터 절전 | 재시작   이 서버 절전 (한 번 묻기) / 재시작 (두 번 묻기)
  컴퓨터 <이름> 절전 | 재시작 | 깨워   등록한 컴퓨터 절전 (한 번 묻기) / 재시작 (두 번 묻기) / 깨우기 (문장도 됨: 컴퓨터 소니 좀 재시작해줘)
  컴퓨터 등록 <이름> <사용자@내부주소>   컴퓨터 등록 (DM, 등록한 서버·채널)
  컴퓨터 수정 <이름> <사용자@내부주소>   접속 정보 바꾸기 (DM, 등록한 서버·채널)
  컴퓨터 삭제 <이름> | 이름변경 <이름> <새 이름>
  컴퓨터 <이름> 수정 | 삭제   전원과 파일에 같은 이름이 있으면 어느 쪽인지 물어요
  컴퓨터 기본 <이름>   !파일 명령이 쓸 기본 앱 컴퓨터
  확인             대기 중인 절전·재시작 진행 (30초 내, 네·어·yes 도 됨)
  초기화           대화 기억 지우기
  도움말           이 안내"""

# 서버를 조회하거나 절전하는 명령. 관리자가 아닌 요청(allow_admin=False)에서는 거부한다
SERVER_COMMANDS = {"상태", "디스크", "메모리", "가동시간", "도커", "점검", "서비스", "컴퓨터", "확인", "DB", "디비", "SQL"}
MAX_HISTORY = 8  # 질문/답변 쌍이 아니라 메시지 개수
NOTE_CONFIRM_SECONDS = 30  # 메모 삭제를 요청한 뒤 확인을 기다리는 시간
EVENT_CONFIRM_SECONDS = 30  # 일정 삭제를 요청한 뒤 확인을 기다리는 시간
BULK_CONFIRM_SECONDS = 30  # 전체 삭제의 두 번 묻기에서 한 번마다 기다리는 시간. 지나면 지우지 않는다
BULK_SHOW = 20  # 전체 삭제를 물을 때 보여 주는 줄 수
SCHEDULE_ASK_SECONDS = 30  # 일정 등록 전에 오전·오후나 이번주·다음주 대답을 기다리는 시간. 지나면 등록하지 않는다
MODE_SECONDS = 20  # 일정을 등록한 뒤 알림 방식(1, 2) 대답을 기다리는 시간. 지나면 저장해 둔 기본값(1번)대로 간다
WAIT_SECONDS = 300  # 봇이 질문하고 답을 기다리는 시간 (지나면 대기를 푼다)
ACTION_RESULT_SECONDS = 600  # 앱에 보낸 파일 동작의 결과 보고를 받는 시간
CONFIRM_SECONDS = 30  # 절전 확인 시간, 한 번 물을 때마다 (/v1 의 confirm 도 같다)
RESULT_WORDS = {"done": "완료", "rejected": "사용자가 취소", "failed": "실패", "expired": "확인 시간 초과"}
PLACE_KEY = "기본위치"
COMPUTER_EDIT_MENU = "1. 전원 주소 2. 전원 이름 3. 파일 이름 4. 둘 다 이름"
COMPUTER_RENAME_MENU = "1. 전원 이름 2. 파일 이름 3. 둘 다 이름"
COMPUTER_DELETE_MENU = "1. 전원만 2. 파일만 3. 둘 다"
COMPUTER_FILE_DELETE_MENU = "파일 등록을 지울까요? 1. 지우기"
DELETE_COMPUTER = 0  # pending 의 '몇 번째 질문' 자리에 넣는 값: 절전이 아니라 컴퓨터 등록 지우기
SLEEP_CONFIRM = 1  # 같은 자리의 값: 절전 질문
RESTART_ASK, RESTART_LAST = 3, 4  # 같은 자리의 값: 재시작의 첫째, 둘째 질문
CONFIRM_ANSWER = "`!확인`이나 `네`를"
CONFIRM_ELSE = "`아니`는 취소하고, 대답이 없거나 다른 말을 하면 아무것도 하지 않아요."
COMPUTER_USAGE = ("사용법: 컴퓨터 [목록] / 컴퓨터 절전 / 컴퓨터 재시작 / 컴퓨터 <이름> 절전 / 컴퓨터 <이름> 재시작 / 컴퓨터 <이름> 깨워 / "
                  "컴퓨터 등록 <이름> <사용자@내부주소> / 컴퓨터 수정 <이름> <사용자@내부주소> (DM, 등록한 서버·채널) / 컴퓨터 삭제 <이름> / 컴퓨터 이름변경 <이름> <새 이름>")
SERVICE_VIEW_ONLY = "서비스는 상태만 볼 수 있어요. 컴퓨터 제어는 `!컴퓨터`로 해 주세요."
COMPUTER_MOVED = "컴퓨터 제어는 `!컴퓨터`로 해 주세요. 예: !컴퓨터 목록, !컴퓨터 절전, !컴퓨터 재시작"
# 메모·일정, 컴퓨터 절전·재시작·삭제 확인을 기다릴 때 받는 짧은 대답
YES_WORDS = {"확인", "어", "어어", "응", "응응", "웅", "네", "네네", "넵", "넹", "예", "그래", "그래요", "좋아", "좋아요", "맞아",
             "해", "해줘", "해 줘", "해주세요", "지워", "지워줘", "지워 줘", "ㅇㅇ", "ㅇㅋ", "오케이", "ok", "okay", "yes", "y"}
NO_WORDS = {"아니", "아니요", "아니오", "아니야", "아냐", "아뇨", "하지마", "하지 마", "취소", "취소해", "취소해줘", "됐어",
            "안해", "안 해", "ㄴㄴ", "no", "n", "nope"}
MODE_ONE_WORDS = {"1", "1번", "기본", "기본값", "그냥", "그냥 해", "그냥해", "그대로", "알아서", "아무거나", "넘어가"} | YES_WORDS
MODE_TWO_WORDS = {"2", "2번", "그 시각에만", "그 시간에만", "그 시각만", "그 시간만", "정각에만", "정각만"}
MODE_CANCEL_WORDS = {"취소", "취소해", "취소해줘", "등록 취소"}
# 삭제 말: 지워, 지워줘, 지워 줘, 지워주세요, 삭제, 삭제해, 삭제해줘, 삭제해 줘, 삭제해주세요, 없애, 없애줘
DELETE_WORD = r"(?:지워(?:\s*줘|주세요)?|삭제(?:해(?:\s*줘|주세요)?)?|없애(?:줘)?)[.!?~ ]*"
NUMBERED_DELETE = rf"(?:#?(\d{{1,9}})\s*번?\s*(?:을|를)?\s*)?{DELETE_WORD}"  # '1 지워', '#1 삭제', '1번 삭제해줘', '지워'
# 목록 줄을 붙인 삭제: '#1 [10-30 17:24] 잠온다 지워줘'(메모), '#1 11월 2일(월) 18:10 치과 이거 지워'(일정)
PASTED_DELETE = (r"#(\d{1,9})\s+(?:(\[\d{2}-\d{2} \d{2}:\d{2}\])|\d{1,2}월 \d{1,2}일\([월화수목금토일]\) \d{2}:\d{2})"
                 rf"\s.*?{DELETE_WORD}")
EVENT_WORDS = ("일정", "알림", "약속")  # 문장으로 일정을 지울 때 있어야 하는 말
# 전체 삭제: 정해진 모양의 문장만 코드가 받는다. 모델에 맡기지 않아 '치과 일정 다 지워줘'가 전체 삭제로 가지 않는다
ALL_WORD = r"(?<![가-힣])(?:전부\s*다|모두\s*다|다|전부|전체|모두)(?:을|를)?\s*"
PAST_WORD = r"지난\s*(?:거|것)?\s*"
BULK_SENTENCE = rf"(?:내\s*)?({PAST_WORD})?(메모|일정|알림|약속)(?:을|를)?\s*{ALL_WORD}{DELETE_WORD}"  # 지난 약속 다 지워줘
BULK_COMMAND = rf"({PAST_WORD})?{ALL_WORD}{DELETE_WORD}"  # !일정 다 지워, !일정 지난 거 다 지워
BULK_BARE = rf"{ALL_WORD}{DELETE_WORD}"  # 다 지워줘
BULK_LIKE = rf"{ALL_WORD}{DELETE_WORD}$"  # 위 모양이 아닌데 끝이 '다 지워줘'인 문장


def _reply_word(text):
    """대답을 비교하기 쉽게 정리한다: 앞뒤 문장 부호와 대소문자, 겹친 공백."""
    return " ".join(text.strip(" .,!?~\n").lower().split())


def _computer_yes(item, word):
    """컴퓨터 확인의 긍정인지. '지워'는 컴퓨터 삭제의 답일 뿐 절전·재시작의 답이 아니다."""
    return word in YES_WORDS and (item[0] == DELETE_COMPUTER or not word.startswith("지워"))


class Assistant:
    def __init__(self):
        self.history = {}
        self.computer_menu = {}  # user -> 수정·삭제 메뉴 대기 (전원과 파일에 같은 이름이 있을 때)
        self.pending = {}  # user -> (몇 번째 질문, 만료시각, 컴퓨터 이름 또는 None)  절전 확인용
        self.pending_note = {}  # user -> (메모 번호, 만료시각)  문장으로 요청한 메모 삭제 확인용
        self.pending_event = {}  # user -> (일정 번호, 만료시각)  일정 삭제 확인용
        self.pending_mode = {}  # user -> (일정 번호, 만료시각)  등록한 일정의 알림 방식 대답 기다리기
        self.pending_bulk = {}  # user -> (종류, 대상 이름, 번호들, 몇 번째 질문, 만료시각)  전체 삭제 확인용
        self.pending_schedule = {}  # user -> (질문, 원래 글자, 만료시각)  등록 전 오전·오후, 이번주·다음주 대답 기다리기
        self.pending_news = {}  # user -> (카테고리 이름, 만료시각)  뉴스 카테고리 삭제 확인용
        self.waiting = {}  # user -> (종류, 자료, 만료시각)  봇이 질문하고 답을 기다리는 중
        self._lock = threading.RLock()  # GPU 하나라 동시에 한 요청씩 처리 (문서 검색은 분류와 호출을 한 번에 잡는다)
        self._ctx = threading.local()  # 요청별 값(모델 이름, 앱 신호). 요청 하나는 한 스레드에서 처리된다
        self.actions = {}  # 앱에 보낸 파일 동작 id -> (user, 동작, 만료시각)  결과 보고 확인용
        self.confirm_ids = {}  # /v1 confirm id -> user  절전 확인용

    def _model(self):
        return getattr(self._ctx, "model", None)  # None 이면 .env 의 ASSISTANT_MODEL

    def _ask(self, messages):
        with self._lock:
            return llm.chat(messages, model=self._model())

    def handle(self, user, text, model=None, allow_admin=True, show_rows=True, private=True, channel=None,
               targets=None):
        """user: 내부 ID. model: 이 요청에서 분류와 대화에 쓸 모델(없으면 .env 값).
        allow_admin: False 이면 서버 조회·제어 명령을 거부한다 (API 입구에서 관리자가 아닌 키).
        show_rows: False 이면 행 내용이 보이는 DB 기능을 거부한다 (등록하지 않은 디스코드 채널).
        private: False 이면 주소가 들어가는 명령을 거부한다 (디스코드는 DM 과 등록한 서버·채널만 True).
        channel: 디스코드 채널에서 온 말이면 {"id", "name"} (뉴스 구독, 채널 관리에 쓴다).
        targets: !채널관리 에 쓴 이름으로 봇이 찾은 채널들."""
        return self.handle_ex(user, text, model, allow_admin, show_rows=show_rows, private=private, channel=channel,
                              targets=targets)["text"]

    def handle_ex(self, user, text, model=None, allow_admin=True, client_actions=False, show_rows=True, private=True,
                  channel=None, targets=None):
        """handle 과 같고, 앱(/v1)에 보낼 것도 함께 돌려준다: {"text", "action", "confirm"}.
        client_actions: True 이면 파일 문장을 client_action 으로, 절전 확인을 confirm 으로 만든다."""
        self._ctx.model, self._ctx.client = model, client_actions
        self._ctx.show_rows = show_rows
        self._ctx.private = private
        self._ctx.channel, self._ctx.targets = channel, targets
        self._ctx.action = self._ctx.confirm = None
        try:
            text = self._handle(user, text, allow_admin)
            return {"text": text, "action": self._ctx.action, "confirm": self._ctx.confirm}
        finally:
            self._ctx.model, self._ctx.client = None, False
            self._ctx.show_rows = False
            self._ctx.private = False
            self._ctx.channel = self._ctx.targets = None
            self._ctx.action = self._ctx.confirm = None

    def _handle(self, user, text, allow_admin):
        text = text.strip()
        is_command = text[:1] in ("!", "/")
        if is_command:
            text = text[1:].strip()
        answer = self._answer_mode(user, text)
        if answer is not None:
            return answer
        answer, asked_notice = self._answer_schedule(user, text)
        if answer is not None:
            return answer
        answer, bulk_notice = self._answer_bulk(user, text)
        if answer is not None:
            return answer
        asked_notice += bulk_notice
        if not text:
            return asked_notice + HELP
        answer, notice = self._answer_pending(user, text)
        if answer is not None:
            return answer
        return asked_notice + notice + self._route(user, text, is_command, allow_admin)

    def _answer_mode(self, user, text):
        """방금 등록한 일정의 알림 방식 대답이면 답을, 아니면 None. 다른 말이나 시간 초과면 저장해 둔 기본값(1번)대로 간다."""
        eid, until = self.pending_mode.pop(user, (None, 0))
        if eid is None or time.time() > until:
            return None
        word = _reply_word(text)
        if word in MODE_CANCEL_WORDS:
            schedule.delete(user, eid)
            return f"일정 #{eid} 등록을 취소했어요."
        if word in MODE_TWO_WORDS:
            schedule.set_mode(user, eid, schedule.MODE_AT_TIME)
            return f"일정 #{eid}은(는) 그 시각에만 알려 드릴게요."
        if not word or word in MODE_ONE_WORDS:
            return f"일정 #{eid}은(는) 미리 알림으로 둘게요. ({schedule.fmt_offsets(schedule.offsets_of(user))} + 그 시각)"
        return None

    def _answer_schedule(self, user, text):
        """일정 등록 전 질문의 대답. (답, 앞에 붙일 안내). 답이 None 이면 평소처럼 처리한다. 다른 말이나 시간 초과면 등록하지 않는다."""
        ask, original, until = self.pending_schedule.pop(user, (None, None, 0))
        if ask is None:
            return None, ""
        word = _reply_word(text).replace(" ", "")
        choice = self._schedule_choice(ask, word)
        if time.time() > until:
            if choice is not None:
                return f"시간이 지나 '{original}' 일정은 등록하지 않았어요. 다시 말씀해 주세요.", ""
            return None, f"(대답이 없어 '{original}' 일정은 등록하지 않았어요.)\n"
        if choice is not None:
            try:
                return self._add_event(user, choice), ""
            except (schedule.ScheduleError, db.DbError) as e:
                return f"⚠️ {e}", ""
        if word in NO_WORDS or word in MODE_CANCEL_WORDS:
            return "일정 등록을 취소했어요.", ""
        return None, "(일정 등록을 취소했어요.)\n"

    @staticmethod
    def _schedule_choice(ask, word):
        """대답 말에 맞는 '다시 해석할 글자' 또는 None. 고를 것이 하나면 네·응도 받는다."""
        choices = ask["choices"]
        if len(choices) == 1 and word in YES_WORDS:
            return choices[0][2]
        for i, (label, _, again) in enumerate(choices, 1):
            if word in (label, str(i), f"{i}번"):
                return again
        return None

    def _answer_bulk(self, user, text):
        """전체 삭제 질문의 대답. (답, 앞에 붙일 안내). 두 번 모두 그렇다고 해야 지운다. 시간 초과나 다른 말이면 지우지 않는다."""
        kind, what, ids, step, until = self.pending_bulk.pop(user, (None, None, None, 0, 0))
        if kind is None:
            return None, ""
        word = _reply_word(text)
        if time.time() > until:
            if word in YES_WORDS:
                return "시간이 지나 전체 삭제를 취소했어요. 아무것도 지우지 않았어요.", ""
            return None, "(대답이 없어 전체 삭제를 취소했어요. 아무것도 지우지 않았어요.)\n"
        if word in NO_WORDS:
            return "전체 삭제를 취소했어요. 아무것도 지우지 않았어요.", ""
        if word not in YES_WORDS:
            return None, "(전체 삭제를 취소했어요. 아무것도 지우지 않았어요.)\n"
        if step == 1:
            self.pending_bulk[user] = (kind, what, ids, 2, time.time() + BULK_CONFIRM_SECONDS)
            return (f"정말 {what} {len(ids)}개를 지울까요? 되돌릴 수 없어요.\n"
                    f"{BULK_CONFIRM_SECONDS}초 안에 `확인`이나 '네'라고 답하면 지워요. 대답이 없으면 지우지 않아요."), ""
        try:
            done = (notes if kind == "메모" else schedule).delete_many(user, ids)
        except db.DbError as e:
            return f"⚠️ {e}", ""
        return f"{what} {done}개를 지웠어요.", ""

    def _answer_pending(self, user, text):
        """메모 삭제·일정 삭제 확인을 기다릴 때의 대답. (답, 앞에 붙일 안내). 답이 None 이면 평소처럼 처리한다.
        컴퓨터 확인보다 먼저 보고, 관리자가 아니어도 된다. 컴퓨터 확인은 긍정이면 _confirm 으로, 그 밖의 말이면 바로 취소한다."""
        word = _reply_word(text)
        category, news_until = self.pending_news.pop(user, (None, 0))
        if category is not None:
            if time.time() > news_until:
                return ("시간이 지나 취소되었어요. 다시 요청해 주세요." if word == "확인" else None), ""
            if word in YES_WORDS:
                try:
                    return news.delete_category(category), ""
                except news.NewsError as e:  # 묻는 사이에 구독이 생긴 경우
                    return f"⚠️ {e}", ""
            if word in NO_WORDS:
                return "카테고리 삭제 요청을 취소했어요.", ""
            return None, "(카테고리 삭제 요청을 취소했어요.)\n"
        note_id, note_until = self.pending_note.pop(user, (None, 0))
        event_id, event_until = self.pending_event.pop(user, (None, 0))
        if note_id is None and event_id is None:
            item = self.pending.get(user)
            if not item or _computer_yes(item, word):  # 긍정은 _confirm 이 처리한다
                return None, ""
            del self.pending[user]
            what = {DELETE_COMPUTER: "컴퓨터 삭제를", RESTART_ASK: "재시작을", RESTART_LAST: "재시작을"}.get(item[0], "절전을")
            if time.time() > item[1]:
                return None, f"(대답이 없어 {what} 취소했어요.)\n"
            if word in NO_WORDS:
                return f"{what} 취소했습니다.", ""
            return None, f"({what} 취소했어요.)\n"
        what = "메모 삭제" if note_id is not None else "일정 삭제"
        if time.time() > (note_until if note_id is not None else event_until):
            return ("시간이 지나 취소되었어요. 다시 요청해 주세요." if word == "확인" else None), ""
        if word in YES_WORDS:
            return (self._delete_note(user, note_id) if note_id is not None else self._delete_event(user, event_id)), ""
        if word in NO_WORDS:
            return f"{what} 요청을 취소했어요.", ""
        return None, f"({what} 요청을 취소했어요.)\n"

    def _route(self, user, text, is_command, allow_admin):
        self._ctx.allow_admin = allow_admin
        if user in self.computer_menu:
            answer = self._answer_computer_menu(user, text, is_command)
            if answer is not None:
                return answer
        if allow_admin and user in self.pending and _computer_yes(self.pending[user], _reply_word(text)):
            text, is_command = "확인", True  # 컴퓨터 확인 대기 중일 때만 '!' 없는 긍정을 받는다
        try:
            if user in self.waiting:
                answer = self._answer_waiting(user, text, is_command)
                if answer is not None:
                    return answer
        except weather.WeatherError as e:
            return f"⚠️ {e}"
        cmd, rest = (text.split(None, 1) + [""])[:2]  # 줄바꿈으로 시작하는 긴 SQL 도 받는다
        rest = rest.strip()
        if cmd.upper() in ("DB", "SQL"):
            cmd = cmd.upper()
        try:
            fn = {
                "도움말": self._help, "help": self._help,
                "메모": self._memo, "메모목록": self._list, "메모찾기": self._find,
                "메모삭제": self._delete, "메모요약": self._memo_summary,
                "요약": self._summary, "날짜": self._date, "날씨": self._weather, "초기화": self._reset,
                "기본위치": self._default_place,
                "일정": self._event, "일정목록": self._event_list, "일정삭제": self._event_delete,
                "알림": self._event, "알림목록": self._event_list, "알림삭제": self._event_delete,  # 일정과 같은 것
                "분전설정": self._offsets, "분전알림": self._offsets, "분전": self._offsets,
                "브리핑": self._briefing, "뉴스": self._news, "채널관리": self._channel_manage, "문서": self._document,
                "상태": self._sys_status, "디스크": self._sys("disk"),
                "메모리": self._sys("mem"), "가동시간": self._sys("uptime"),
                "도커": self._sys("docker"), "점검": self._checkup,
                "서비스": self._service, "컴퓨터": self._computer, "확인": self._confirm,
                "DB": self._db, "디비": self._db, "SQL": self._sql,
            }.get(cmd)
            admin_only = cmd in SERVER_COMMANDS or (cmd, rest) == ("문서", "색인") or cmd == "채널관리" or (cmd == "뉴스" and news.needs_admin(rest))
            if is_command and fn and admin_only and not allow_admin and cmd != "컴퓨터":
                return "이 명령은 관리자만 쓸 수 있어요."
            if is_command and fn:
                return fn(user, rest)
            if is_command:
                return f"모르는 명령어예요: {cmd}\n\n" + HELP
            return self._free_sentence(user, text)
        except (llm.LLMError, weather.WeatherError, classifier.ClassifierError, schedule.ScheduleError,
                briefing.BriefingError, documents.DocumentError, news.NewsError, db.DbError) as e:
            return f"⚠️ {e}"

    # --- 자유 문장: 분류기 ---
    def _classify(self, text):
        """분류 결과(검증됨) 또는 None. 형식이 틀리면 한 번 다시 시도한다. 접속 오류는 ClassifierError."""
        for _ in range(2):
            with self._lock:
                result, _, _ = classifier.classify(text, model=self._model())
            if result is not None:
                return result
        return None

    def _free_sentence(self, user, text):
        pasted = re.fullmatch(PASTED_DELETE, text.strip())
        if pasted:  # 목록 줄의 형식으로 코드가 메모인지 일정인지 가른다
            if pasted.group(2):
                return self._ask_delete_note(user, int(pasted.group(1)))
            return self._event_delete(user, pasted.group(1))
        answer = self._bulk_sentence(user, text.strip())
        if answer is not None:
            return answer
        if files.looks_like_file(text):  # 파일 말이 있으면 파일 지시문으로 먼저 묻는다. 파일 요청이 아니면 평소 분류로
            f = self._classify_file(text)
            if f is not None and f["동작"] != "아님":
                return self._file(user, f)
        if documents.starts_with_document(text):  # 맨 앞이 "문서"면 문서 검색을 먼저 해 본다
            d = self._documents(documents.strip_start(text))
            if d["kind"] != "not_found":
                return d["text"]
            return f"{documents.NOT_FOUND_PREFIX}\n{self._normal_sentence(user, text)}"
        return self._normal_sentence(user, text)

    def _normal_sentence(self, user, text):
        """평소 분류기 (날씨·메모·일정·일반대화)."""
        r = self._classify(text)
        if r is None:
            if classifier.looks_like_weather(text):
                return "무엇을 물으셨는지 이해하지 못했어요. '부산 날씨', '내일 우산 필요해?'처럼 다시 말씀해 주세요."
            if classifier.looks_like_memo(text):
                return "메모 요청인지 이해하지 못했어요. '내일 치과 3시 메모해줘'처럼 다시 말씀하시거나 `!메모 내용`으로 저장해 주세요."
            if classifier.looks_like_schedule(text):
                return "일정 요청인지 이해하지 못했어요. '내일 15시 치과 일정 잡아줘'처럼 다시 말씀하시거나 `!일정 내일 15시 치과`로 등록해 주세요."
            return self._chat(user, text)
        intent = r["의도"]
        if intent == "날씨":
            return self._weather_answer(user, r["지역"], r["날짜"], r.get("날짜글자"))
        if intent == "메모저장":
            return self._save_memo(user, r["내용"])
        if intent == "메모조회":
            return self._find(user, r["내용"]) if r["내용"] else self._list(user, "")
        if intent == "메모요약":
            return self._memo_summary(user, "")
        if intent in classifier.DELETE_INTENTS:
            return self._delete_sentence(user, text, r["내용"])
        if intent == "일정등록" and classifier.is_schedule_request(text, r["내용"]):
            return self._add_event(user, r["내용"])  # 날짜·시각 해석과 등록 흐름은 명령어와 같다
        if intent == "일정조회":
            return self._event_list(user, "")
        return self._chat(user, text)

    # --- 파일: 서버는 의도만 보내고 실행은 앱이 한다 ---
    def _classify_file(self, text):
        with self._lock:
            return files.classify(text, model=self._model())

    def match_file_names(self, query, candidates):
        """앱의 기존 이름 비교도 같은 GPU 잠금을 사용한다. 대화 기억에는 넣지 않는다."""
        with self._lock:
            return files.match_names(query, candidates)

    def _file(self, user, r):
        if not getattr(self._ctx, "client", False):
            return ("파일 작업은 데스크톱 앱에서 말씀해 주세요. 서버는 내 컴퓨터의 파일을 볼 수 없어서 앱이 대신 해요.\n"
                    "(디스코드에서는 앱의 봇에 '파일 찾기 보고서'처럼 보내면 돼요)")
        action, say = files.build(r)
        if action:
            now = time.time()
            self.actions = {k: v for k, v in self.actions.items() if v[2] > now}  # 오래된 것 정리
            self.actions[action["id"]] = (user, action["action"], now + ACTION_RESULT_SECONDS)
            self._ctx.action = action
        return say

    def record_result(self, user, action_id, status, detail=""):
        """앱이 보낸 파일 동작 결과를 대화 기억에 남긴다. 모르는 id(또는 다른 사람의 것)면 False."""
        item = self.actions.get(action_id)
        if item is None or item[0] != user or item[2] < time.time():
            return False
        del self.actions[action_id]
        note = f"(파일 작업 결과: {RESULT_WORDS.get(status, status)}{f' - {detail}' if detail else ''})"
        hist = self.history.setdefault(user, [])
        hist.append({"role": "assistant", "content": note})
        del hist[:-MAX_HISTORY]
        return True

    def confirm_v1(self, user, confirm_id, approve):
        """/v1/confirm: (성공 여부, 글, 다음 확인 창 또는 None). 없는 id·다른 사람·시간 초과면 (False, 안내, None)."""
        if self.confirm_ids.pop(confirm_id, None) != user:
            return False, "시간이 지났거나 없는 요청입니다. 다시 요청해 주세요.", None
        item = self.pending.get(user)
        if item is None or time.time() > item[1]:
            self.pending.pop(user, None)
            return False, "시간이 지나 취소되었습니다. 다시 요청해 주세요.", None
        if not approve:
            self.pending.pop(user, None)
            return True, "취소했습니다.", None
        self._ctx.client, self._ctx.confirm = True, None
        try:
            return True, self._confirm(user, ""), self._ctx.confirm
        finally:
            self._ctx.client, self._ctx.confirm = False, None

    # --- 문장으로 요청한 삭제: 무엇을 지울지는 문장의 말로 코드가 정하고, 확인을 거친다 ---
    def _delete_sentence(self, user, text, number):
        is_memo, is_event = "메모" in text, any(w in text for w in EVENT_WORDS)
        if is_memo == is_event:  # 둘 다 없거나 둘 다 있으면 지우지 않는다
            return "메모인지 일정인지 함께 말씀해 주세요. 예: 3번 메모 지워줘, 1번 일정 지워줘"
        if is_memo:
            return self._ask_delete_note(user, number)
        return self._event_delete(user, str(number) if number else "")

    # --- 전체 삭제: 정해진 문장과 명령만 받고, 두 번 묻는다. 물을 때 보여 준 것만 지운다 ---
    def _bulk_sentence(self, user, text):
        """전체 삭제를 말한 문장이면 답을, 아니면 None. 번호가 있으면 그 번호만 지우도록 평소 분류로 보낸다."""
        if re.fullmatch(BULK_BARE, text):
            return "메모인지 일정인지 함께 말씀해 주세요. 예: 메모 다 지워줘, 약속 다 지워줘"
        m = re.fullmatch(BULK_SENTENCE, text)
        if m:
            return self._ask_delete_all(user, "메모" if m.group(2) == "메모" else "일정", bool(m.group(1)))
        if (re.search(BULK_LIKE, text) and not re.search(r"\d+\s*번|#\d+", text)
                and any(w in text for w in ("메모", *EVENT_WORDS))):
            return ("전체 삭제는 '약속 다 지워줘', '지난 약속 다 지워줘', '메모 다 지워줘'처럼 말씀해 주세요.\n"
                    "일부만 지우려면 목록의 번호로 지워 주세요. 예: 1번 일정 지워줘")
        return None

    def _ask_delete_all(self, user, kind, past=False):
        if kind == "메모":
            if past:
                return "메모는 '지난' 기준이 없어요. '메모 다 지워줘'나 '3번 메모 지워줘'처럼 말씀해 주세요."
            rows, fmt, what, none = notes.everything(user), notes.fmt, "메모", "지울 메모가 없습니다."
        else:
            rows = schedule.past(user) if past else schedule.everything(user)
            fmt, what = schedule.fmt, "지난 일정" if past else "일정"
            none = f"지울 {what}이 없습니다."
        if not rows:
            return none
        more = f"\n... 외 {len(rows) - BULK_SHOW}개" if len(rows) > BULK_SHOW else ""
        self.pending_bulk[user] = (kind, what, [r[0] for r in rows], 1, time.time() + BULK_CONFIRM_SECONDS)
        return (f"{what} {len(rows)}개를 모두 지울까요?\n{fmt(rows[:BULK_SHOW])}{more}\n"
                f"{BULK_CONFIRM_SECONDS}초 안에 `확인`이나 '네'라고 답해 주세요. 한 번 더 물어봐요. 대답이 없으면 지우지 않아요.")

    def _ask_delete_note(self, user, note_id):
        if note_id is None:
            rows = notes.recent(user, 10)
            if not rows:
                return "저장된 메모가 없습니다."
            return f"{notes.fmt(rows)}\n몇 번 메모를 지울지 알려 주세요. 예: 3번 메모 지워줘"
        row = notes.get(user, note_id)
        if row is None:
            return "해당 번호의 메모가 없습니다."
        self.pending_event.pop(user, None)
        self.pending_note[user] = (note_id, time.time() + NOTE_CONFIRM_SECONDS)
        return (f"이 메모를 지울까요? 지우면 되돌릴 수 없어요.\n{notes.fmt([row])}\n"
                f"{NOTE_CONFIRM_SECONDS}초 안에 `확인`이나 '네'라고 답하면 지워요. 다른 말을 하면 취소돼요.")

    def _delete_note(self, user, note_id):
        return f"메모 #{note_id} 삭제했습니다." if notes.delete(user, note_id) else "해당 번호의 메모가 없습니다."

    # --- 날씨와 기본 위치 ---
    def _weather_answer(self, user, place, day, phrase=None):
        """place 가 없으면 사용자 기본 위치, 그것도 없으면 물어보고 기다린다. day 가 None 이면 날짜로 바꿀 수 없는 말."""
        if day is None:
            asked = f"'{phrase}'은(는) " if phrase else ""
            return (f"{asked}날짜로 알 수 없어요. 날짜로 말씀해 주세요 (예: 10월 23일, 23일).\n"
                    "오늘·내일·모레는 그대로 말씀하셔도 돼요.")
        if not place:
            place = users.get_setting(user, PLACE_KEY)
        if not place:
            self.waiting[user] = ("기본위치", {"day": day}, time.time() + WAIT_SECONDS)
            return ("기본 위치가 아직 없어요. 날씨를 볼 기본 지역을 알려 주세요 (예: 판교, 부산 해운대구).\n"
                    "앞으로 지역을 말하지 않으면 이 위치로 알려 드려요.")
        return weather.report_place(place, day)

    def _answer_waiting(self, user, text, is_command):
        """기다리던 답이면 처리한 결과, 아니면 None (대기를 풀고 평소처럼 처리)."""
        kind, data, expires = self.waiting.pop(user)
        if time.time() > expires or is_command or kind != "기본위치":
            return None
        place = text.strip().strip("?!.~ ")
        if not place or len(place) > 15 or len(place.split()) > 3:
            return None  # 지역 이름이 아니라 다른 말을 한 것으로 본다
        msg = weather.ambiguous_message(place)
        if msg:
            self.waiting[user] = (kind, data, time.time() + WAIT_SECONDS)
            return msg
        if not weather.resolve(place):
            if data.get("retried"):
                return None  # 두 번째도 못 찾으면 대기를 풀고 평소 문장으로 처리한다
            self.waiting[user] = (kind, dict(data, retried=True), time.time() + WAIT_SECONDS)
            return f"'{place}' 지역을 찾지 못했어요. 다른 이름으로 다시 알려 주세요."
        users.set_setting(user, PLACE_KEY, place)
        return f"기본 위치를 '{place}'(으)로 저장했어요.\n" + weather.report_place(place, data["day"])

    def _default_place(self, user, rest):
        place = rest.strip()
        if not place:
            cur = users.get_setting(user, PLACE_KEY)
            return (f"기본 위치: {cur}" if cur else "기본 위치가 없습니다.") + "\n바꾸려면: 기본위치 <지역>  (예: 기본위치 판교)"
        msg = weather.ambiguous_message(place)
        if msg:
            return msg
        found = weather.resolve(place)
        if not found:
            return weather.not_found_message(place)
        users.set_setting(user, PLACE_KEY, place)
        self.waiting.pop(user, None)
        return f"기본 위치를 '{place}'(으)로 저장했어요. (찾은 위치: {found[0]})"

    # --- 코드만으로 처리 (모델 불필요) ---
    def _help(self, user, rest):
        return HELP

    def _date(self, user, rest):
        return llm.date_text()

    def _weather(self, user, rest):
        place, day = weather.parse_query(f"날씨 {rest}")
        return self._weather_answer(user, place, day)

    def _reset(self, user, rest):
        self.history.pop(user, None)
        return "대화 기억을 지웠습니다."

    def _memo(self, user, rest):
        """메모 <내용> / 메모 목록 [개수] / 메모 찾기 <단어> / 메모 삭제 <번호>. 그 밖의 글자는 내용으로 저장한다."""
        if not rest:
            return "저장할 내용을 적어주세요. 예: 메모 내일 치과 3시"
        word, _, more = rest.partition(" ")
        more = more.strip()
        if word == "목록" and (not more or more.isdigit()):
            return self._list(user, more)
        if word == "찾기":
            return self._find(user, more)
        if word == "삭제" and more.lstrip("#").isdigit():
            return self._delete(user, more.lstrip("#"))
        bulk = re.fullmatch(BULK_COMMAND, rest)
        if bulk:
            return self._ask_delete_all(user, "메모", bool(bulk.group(1)))
        numbered = re.fullmatch(NUMBERED_DELETE, rest)
        if numbered:
            return self._ask_delete_note(user, int(numbered.group(1)) if numbered.group(1) else None)
        return self._save_memo(user, rest)

    def _save_memo(self, user, text):
        return f"메모 #{notes.add(user, text)} 저장했습니다."

    def _list(self, user, rest):
        n = int(rest) if rest.isdigit() else 10
        rows = notes.recent(user, n)
        return notes.fmt(rows) if rows else "저장된 메모가 없습니다."

    def _find(self, user, rest):
        if not rest:
            return "검색할 단어를 적어주세요. 예: 메모 찾기 치과"
        rows = notes.search(user, rest)
        return notes.fmt(rows) if rows else f"'{rest}'이(가) 들어간 메모가 없습니다."

    def _delete(self, user, rest):
        if not rest.isdigit():
            return "삭제할 메모 번호를 적어주세요. 예: 메모 삭제 3"
        return "삭제했습니다." if notes.delete(user, int(rest)) else "해당 번호의 메모가 없습니다."

    # --- 일정: 날짜·시각은 코드가 해석한다. 애매하면 먼저 묻고, 등록하면 알림 방식을 묻고, 삭제는 확인을 거친다 ---
    def _event(self, user, rest):
        """일정 <날짜> <시각> <내용> / 일정 목록 / 일정 삭제 <번호> / 일정 <번호> 지워."""
        if not rest or rest == "목록":
            return self._event_list(user, "")
        word, _, more = rest.partition(" ")
        if word == "삭제" and (not more.strip() or more.strip().lstrip("#").isdigit()):
            return self._event_delete(user, more.strip())
        bulk = re.fullmatch(BULK_COMMAND, rest)
        if bulk:
            return self._ask_delete_all(user, "일정", bool(bulk.group(1)))
        numbered = re.fullmatch(NUMBERED_DELETE, rest)
        if numbered:
            return self._event_delete(user, numbered.group(1) or "")
        return self._add_event(user, rest)

    def _add_event(self, user, rest):
        r = schedule.parse(rest)
        if r["ask"]:
            return self._ask_schedule(user, r["ask"], rest)
        eid = schedule.add(user, r["at"], r["text"], mode=schedule.MODE_BEFORE)
        after = f" ({r['after']} 뒤)" if r["after"] else ""
        head = f"📅 일정(알림) #{eid} 등록했어요: {schedule.fmt_at(r['at'])} {r['text']}{after}"
        offsets = schedule.applicable_offsets(user, r["at"])
        if not offsets:  # 미리 알릴 시점이 없으니 물어볼 것도 없다
            return f"{head}\n시각이 되면 디스코드 DM으로 알려 드려요."
        self.pending_mode[user] = (eid, time.time() + MODE_SECONDS)
        return (f"{head}\n알림 방식을 {MODE_SECONDS}초 안에 골라 주세요. 대답이 없으면 1번으로 둬요.\n"
                f"1. 미리 알림: {schedule.fmt_offsets(offsets)} + 그 시각\n2. 그 시각에만")

    def _ask_schedule(self, user, ask, original):
        self.pending_schedule[user] = (ask, original, time.time() + SCHEDULE_ASK_SECONDS)
        lines = [f"{i}. {shown}" for i, (_, shown, _) in enumerate(ask["choices"], 1) if shown]
        return "\n".join([f"{ask['question']} {SCHEDULE_ASK_SECONDS}초 안에 답해 주세요. 대답이 없으면 등록하지 않아요.", *lines])

    def _event_list(self, user, rest):
        rows = schedule.upcoming(user)
        if not rows:
            return "앞으로의 일정이 없습니다. 등록: 일정 내일 15시 치과"
        return "📅 앞으로의 일정(알림)\n" + schedule.fmt(rows)

    def _event_delete(self, user, rest):
        if not rest:
            rows = schedule.upcoming(user)
            if not rows:
                return self._event_list(user, "")
            return f"{self._event_list(user, '')}\n몇 번 일정을 지울지 알려 주세요. 예: 1번 일정 지워줘"
        if not rest.lstrip("#").isdigit():
            return "삭제할 일정 번호를 적어주세요. 번호는 '일정 목록'으로 볼 수 있어요. 예: 일정 삭제 3"
        row = schedule.get(user, int(rest.lstrip("#")))
        if row is None:
            return "해당 번호의 일정이 없습니다."
        self.pending_note.pop(user, None)
        self.pending_event[user] = (row[0], time.time() + EVENT_CONFIRM_SECONDS)
        return (f"이 일정을 지울까요?\n{schedule.fmt([row])}\n"
                f"{EVENT_CONFIRM_SECONDS}초 안에 `확인`이나 '네'라고 답하면 지워요. 다른 말을 하면 취소돼요.")

    def _delete_event(self, user, event_id):
        return f"일정 #{event_id} 삭제했습니다." if schedule.delete(user, event_id) else "해당 번호의 일정이 없습니다."

    def _offsets(self, user, rest):
        """분전설정 [시점]: 미리 알림 시점 보기 / 바꾸기 / '기본'으로 되돌리기. '!분전 설정 …'처럼 띄어 써도 된다."""
        word, _, more = rest.partition(" ")
        if word in ("설정", "알림"):
            rest = more.strip()
        if rest in ("기본", "기본값"):
            schedule.set_offsets(user, schedule.DEFAULT_OFFSETS)
        elif rest:
            schedule.set_offsets(user, schedule.parse_offsets(rest))
        else:
            return (f"미리 알림: {schedule.fmt_offsets(schedule.offsets_of(user))} + 그 시각\n"
                    "바꾸려면: 분전설정 30분, 20분, 10분, 5분  (1번을 고른 일정은 모두 이 설정을 따라요)")
        return f"미리 알림을 바꿨어요: {schedule.fmt_offsets(schedule.offsets_of(user))} + 그 시각"

    # --- 문서 검색: 비서가 색인한 조각을 찾고 비서 모델이 답한다 (5_1_4 17절, 같은 GPU를 쓰므로 한 번에 한 요청) ---
    def _doc_classify(self, text):
        """문서 전용 분류기. 실패하면 None (코드 기본 경로로 처리한다)."""
        try:
            return documents.classify(text, model=self._model())
        except classifier.ClassifierError:
            return None

    def _doc_answer(self, system, question):
        """문서 답: 앱이 고른 모델이면 그 모델, 아니면 ASSISTANT_MODEL."""
        return llm.chat([{"role": "user", "content": question}], model=self._model(),
                        system=system, max_tokens=config.DOC_MAX_TOKENS)

    def _documents(self, text):
        with self._lock:
            return documents.handle(text, classify_fn=self._doc_classify, answer=self._doc_answer)

    def _document(self, user, rest):
        if rest == "색인":  # 뒤에 말이 더 있으면 검색이다 ('!문서 색인 규칙 알려줘')
            with self._lock:  # 임베딩도 GPU를 쓴다. 색인이 끝날 때까지 다른 요청은 기다린다
                return documents.index_now()
        return self._documents(rest)["text"]  # !문서 는 못 찾아도 일반 대화로 넘기지 않는다

    # --- 디스코드 채널 관리: 지정 명령은 여기로, 관리 채널의 말은 channel_request 로 ---
    def _channel_manage(self, user, rest):
        return channel_manage.command(rest, getattr(self._ctx, "channel", None), getattr(self._ctx, "targets", None))

    def channel_request(self, channel_id, message_id, author_id, text):
        return channel_manage.request(channel_id, message_id, author_id, text, self._lock)

    # --- 뉴스: 코드가 가져오고 모델은 부르지 않는다 ---
    def _news(self, user, rest):
        words = rest.split()
        if words[:2] == ["카테고리", "삭제"]:
            if len(words) != 3:
                return "사용법: !뉴스 카테고리 삭제 <이름>"
            name = news.check_deletable(words[2])
            self.pending_news[user] = (name, time.time() + NOTE_CONFIRM_SECONDS)
            return f"'{name}' 카테고리와 그 출처, 관심어를 지울까요? 네 / 아니오"
        return news.run(rest, getattr(self._ctx, "channel", None))

    # --- 아침 브리핑: 값은 코드가 모은다. 모델은 쓰지 않는다 ---
    def briefing_text(self, user):
        """브리핑 글 (입구의 자동 발송도 이것을 부른다)."""
        return briefing.build(user)

    def _briefing(self, user, rest):
        word, _, more = rest.partition(" ")
        if not rest:
            return briefing.build(user)
        if word == "켜기":
            hhmm = briefing.turn_on(user, briefing.parse_hhmm(more) if more.strip() else None)
            return f"매일 {hhmm}에 디스코드로 브리핑을 보내 드려요. 끄려면: 브리핑 끄기"
        if word == "끄기":
            briefing.turn_off(user)
            return "아침 브리핑을 껐어요. 다시 켜려면: 브리핑 켜기 [시각]"
        if word == "상태":
            t = briefing.time_of(user)
            return f"아침 브리핑: 매일 {t}" if t else "아침 브리핑이 꺼져 있어요. 켜려면: 브리핑 켜기 [시각]"
        return "사용법: 브리핑 (지금 보기) / 브리핑 켜기 [시각] / 브리핑 끄기 / 브리핑 상태"

    # --- DB 보기: 쓰기는 없다. 행 내용은 볼 수 있는 곳에서만 ---
    def _show_rows(self):
        return getattr(self._ctx, "show_rows", False)

    def _db(self, user, rest):
        return dbinfo.command(rest, self._show_rows())

    def _sql(self, user, rest):
        if not self._show_rows():
            return dbinfo.ROWS_PLACE_ONLY
        return dbinfo.run_sql(rest) if rest else "사용법: SQL SELECT id, text FROM notes LIMIT 3  (읽기 전용)"

    # --- 서버 관리 (실행 여부는 코드가 결정, 모델 불필요) ---
    def _sys_status(self, user, rest):
        return sysadmin.get_system_status()

    def _sys(self, key):
        return lambda user, rest: sysadmin.run_safe(key)

    def _service(self, user, rest):
        parts = sysadmin.clean_words(rest, {*sysadmin.STATUS_SERVICES, *sysadmin.CONTAINER_SERVICES})
        if parts and (parts[0] == "컴퓨터" or parts[0] in sysadmin.RESTART_WORDS
                      or any(p in sysadmin.SLEEP_WORDS or p in sysadmin.WAKE_WORDS for p in parts)):
            return COMPUTER_MOVED  # 컴퓨터 제어는 !컴퓨터 로 옮겼다. 아무것도 실행하지 않는다
        parts = [p for p in parts if p not in sysadmin.VIEW_WORDS]  # 'nginx 상태 알려줘'
        if not parts or parts == ["목록"]:
            return sysadmin.service_list()
        if any(p in sysadmin.RESTART_WORDS for p in parts):
            return SERVICE_VIEW_ONLY  # 나중에 서비스 재시작이 쓸 자리
        action = sysadmin.ACTIONS.get(parts[1]) if len(parts) > 1 else "status"
        if action is None:
            return f"알 수 없는 동작입니다: {parts[1]} (상태만 볼 수 있어요)"
        if action != "status":
            return SERVICE_VIEW_ONLY
        return sysadmin.service_status(parts[0])

    def _computer(self, user, rest):
        """컴퓨터 전원과 등록, 앱 컴퓨터(파일). 문장은 모델 없이 코드가 고른다: 이름 하나와 동작 하나만 남을 때 실행하고, 모르는 말이 남으면 실행하지 않는다.
        등록·수정·삭제·이름변경의 값은 명령 말과 겹쳐도 그 명령에서 검사한다."""
        raw = rest.split()
        admin = getattr(self._ctx, "allow_admin", True)
        if len(raw) == 2 and raw[1] in ("수정", "삭제"):  # !컴퓨터 소니 수정
            raw = [raw[1], raw[0]]
        answer = self._computer_files(user, raw, admin)
        if answer is not None:
            return answer
        if not admin:
            return "이 명령은 관리자만 쓸 수 있어요. 앱 컴퓨터는 `!컴퓨터`, `!컴퓨터 기본 <이름>`, `!컴퓨터 삭제 <이름>`, `!컴퓨터 이름변경 <이름> <새 이름>`으로 다뤄요."
        if raw and raw[0] in ("등록", "수정", "삭제", "이름변경"):
            return self._computer_admin(user, [p for p in raw if p != "줘"])
        parts = sysadmin.clean_words(rest, {*sysadmin.computers(), sysadmin.THIS_SERVER})
        restarts = [p for p in parts if p in sysadmin.RESTART_WORDS]
        if restarts:
            return self._restart_order(user, self._computer_target(parts, restarts[0]))
        words = [p for p in parts if p in sysadmin.SLEEP_WORDS or p in sysadmin.WAKE_WORDS]
        if words:
            return self._computer_order(user, words[0], self._computer_target(parts, words[0]))
        if [p for p in parts if p not in sysadmin.SHOW_WORDS] in ([], ["목록"]):
            return sysadmin.computer_list(self._file_states(user))
        return f"무슨 말인지 몰라 실행하지 않았어요.\n{sysadmin.computer_list(self._file_states(user))}"

    @staticmethod
    def _file_states(user):
        default = app_computers.default_of(user)
        return {r["name"]: app_computers.status_word(r["id"]) + (" (기본)" if default and r["id"] == default["id"] else "")
                for r in app_computers.list_for(user)}

    def _file_list(self, user):
        states = self._file_states(user)
        if not states:
            return "등록한 앱 컴퓨터가 없어요. 앱 설정 > 원격 > 비서 서버 연결에서 등록해 주세요."
        return "\n".join(["컴퓨터 목록", *(f"- {name}: {state}" for name, state in states.items())])

    def _computer_files(self, user, raw, admin):
        """앱 컴퓨터가 관련된 말이면 답, 전원만의 일이면 None. 양쪽에 있는 이름은 어느 쪽인지 묻는다."""
        power = sysadmin.computers() if admin else {}
        if not admin and (not raw or raw == ["목록"]) and app_computers.list_for(user):
            return self._file_list(user)
        if raw and raw[0] == "기본":
            found = app_computers.by_name(user, raw[1]) if len(raw) == 2 else None
            if found is None:
                return f"기본으로 할 앱 컴퓨터 이름을 적어 주세요.\n{self._file_list(user)}"
            app_computers.set_default(user, found["id"])
            return f"{found['name']}을(를) 기본 컴퓨터로 정했어요. `!파일 이동 …`은 이 컴퓨터에서 실행해요."
        if len(raw) < 2 or raw[0] not in ("수정", "삭제", "이름변경"):
            return None
        word, name = raw[0], raw[1]
        found = app_computers.by_name(user, name)
        if found is None:
            return None  # 전원 쪽 명령으로 그대로 처리한다
        both = name in power
        if word == "삭제" and len(raw) == 2:
            return self._ask_computer_menu(user, "delete", found, COMPUTER_DELETE_MENU if both else COMPUTER_FILE_DELETE_MENU, both)
        if word == "수정" and len(raw) == 2:
            if both:
                return self._ask_computer_menu(user, "edit", found, COMPUTER_EDIT_MENU, both)
            return f"앱 컴퓨터는 이름만 바꿀 수 있어요: !컴퓨터 이름변경 {found['name']} <새 이름>"
        if word == "이름변경" and len(raw) == 3:
            if both:
                return self._ask_computer_menu(user, "rename", found, COMPUTER_RENAME_MENU, both, raw[2])
            return self._rename_file_computer(user, found, raw[2])
        return None

    def _ask_computer_menu(self, user, kind, found, menu, both, value=None):
        self.computer_menu[user] = {"kind": kind, "found": found, "both": both, "value": value, "step": "pick",
                                    "until": time.time() + CONFIRM_SECONDS}
        return f"{found['name']}: {menu}\n{CONFIRM_SECONDS}초 안에 번호를 보내 주세요. 다른 말을 하면 취소해요."

    def _answer_computer_menu(self, user, text, is_command):
        """수정·삭제 메뉴의 답. 메뉴의 답이 아니면 None (메뉴를 닫고 평소처럼 처리)."""
        menu = self.computer_menu.pop(user)
        word = text.strip()
        if time.time() > menu["until"] or (is_command and not word.isdigit()):  # 채널에서는 !1 로 답한다
            return None
        name, kind = menu["found"]["name"], menu["kind"]
        if menu["step"] == "value":
            return self._computer_menu_value(user, menu, word)
        if kind == "delete":
            return self._computer_menu_delete(user, menu, word)
        if kind == "rename":
            pick = {"1": "power", "2": "file", "3": "both"}.get(word)
            if pick is None:
                return None
            menu["pick"] = pick
            return self._computer_menu_value(user, menu, menu["value"])
        pick = {"1": "address", "2": "power", "3": "file", "4": "both"}.get(word)
        if pick is None:
            return None
        self.computer_menu[user] = {**menu, "step": "value", "pick": pick, "until": time.time() + CONFIRM_SECONDS}
        return f"{name}의 새 주소(사용자@내부주소)를 보내 주세요." if pick == "address" else f"{name}의 새 이름을 보내 주세요."

    def _computer_menu_delete(self, user, menu, word):
        found = menu["found"]
        picks = {"1": "power", "2": "file", "3": "both"} if menu["both"] else {"1": "file"}
        pick = picks.get(word)
        if pick is None:
            return None
        said = ""
        if pick in ("file", "both"):
            app_computers.send_soon(found["id"], {"type": "rejected", "reason": "unknown_computer"}, 4404)
            app_computers.remove(user, found["id"])
            said = f"{found['name']}의 파일 등록을 지웠어요. 앱의 서버 원격 허용도 꺼져요."
        if pick in ("power", "both"):
            ask = self._ask_delete_computer(user, found["name"])
            return f"{said}\n{ask}" if said else ask
        return said

    def _computer_menu_value(self, user, menu, value):
        found, pick = menu["found"], menu["pick"]
        if pick == "address":
            if not getattr(self._ctx, "private", False):
                return "주소는 DM 이나 등록한 서버·채널에서 보내 주세요. 방금 메시지는 지워 주세요."
            return self._update_computer(found["name"], value)
        if pick == "file":
            return self._rename_file_computer(user, found, value)
        problem = sysadmin.check_name(value)
        if problem:
            return problem
        if value in sysadmin.computers():
            return f"'{value}'은(는) 이미 있어요."
        if pick == "power":
            return self._rename_computer(found["name"], value)
        answer = self._rename_file_computer(user, found, value)  # 둘 다: 파일에서 거절되면 전원은 그대로 둔다
        if app_computers.by_name(user, value) is None or app_computers.by_name(user, value)["id"] != found["id"]:
            return answer
        self._rename_computer(found["name"], value)
        return f"{found['name']}의 전원과 파일 이름을 {value}(으)로 바꿨어요."

    @staticmethod
    def _rename_file_computer(user, found, new):
        try:
            new = app_computers.rename(user, found["id"], new)
        except app_computers.ComputerError as e:
            return str(e)
        return f"{found['name']}의 파일 쪽 이름을 {new}(으)로 바꿨어요."

    @staticmethod
    def _computer_target(parts, word):
        """동작 말을 뺀 나머지. '서버', '이 서버'는 이름 없음(이 서버)과 같다."""
        others = [p for p in parts if p != word]
        return [] if others in ([sysadmin.THIS_SERVER], ["이", sysadmin.THIS_SERVER]) else others

    def _computer_admin(self, user, parts):
        """컴퓨터 [등록 <이름> <사용자@주소> | 수정 <이름> <사용자@주소> | 삭제 <이름> | 이름변경 <이름> <새 이름>]."""
        word, args = parts[0], parts[1:]
        if not sysadmin.config.SYSADMIN_CONTROL:
            return sysadmin.CONTROL_OFF
        if word in ("등록", "수정"):
            if not getattr(self._ctx, "private", False):  # 등록하지 않은 채널 기록에 주소가 남지 않게 한다
                return f"{word}은 내부 주소가 들어가서 DM 이나 등록한 서버·채널에서 해 주세요. 방금 메시지는 지우는 것이 좋아요."
            if len(args) != 2:
                return COMPUTER_USAGE
            return self._add_computer(*args) if word == "등록" else self._update_computer(*args)
        if word == "삭제":
            return self._ask_delete_computer(user, args[0]) if len(args) == 1 else COMPUTER_USAGE
        return self._rename_computer(*args) if len(args) == 2 else COMPUTER_USAGE

    def _add_computer(self, name, target):
        problem = sysadmin.check_name(name) or sysadmin.check_target(target)
        if problem:
            return problem
        if name in sysadmin.computers():
            return f"'{name}'은(는) 이미 있어요. 다른 이름을 쓰거나 지운 뒤 등록해 주세요."
        sysadmin.add_computer(name, target)
        if sysadmin.computer_up(name):
            return f"{name}을(를) 등록했어요. SSH 포트에 접속돼요. 재우려면: 컴퓨터 {name} 재워"
        return (f"{name}을(를) 등록했어요. 다만 지금 SSH 포트에 접속되지 않아요. "
                "켜져 있는지와 SSH 설정을 확인해 주세요 (README.md).")

    def _update_computer(self, name, target):
        if name not in sysadmin.computers():
            return f"'{name}' 컴퓨터는 없어요.\n{sysadmin.computer_list()}"
        problem = sysadmin.check_target(target)
        if problem:
            return problem
        self.pending = {u: v for u, v in self.pending.items() if v[2] != name}  # 옛 주소로 기다리던 확인은 취소한다
        sysadmin.update_target(name, target)
        if sysadmin.computer_up(name):
            return f"{name}의 접속 정보를 바꿨어요. SSH 포트에 접속돼요."
        return (f"{name}의 접속 정보를 바꿨어요. 다만 지금 SSH 포트에 접속되지 않아요. "
                "켜져 있는지와 주소를 확인해 주세요 (README.md).")

    def _ask_delete_computer(self, user, name):
        if name not in sysadmin.computers():
            return f"'{name}' 컴퓨터는 없어요.\n{sysadmin.computer_list()}"
        self.pending[user] = (DELETE_COMPUTER, time.time() + CONFIRM_SECONDS, name)
        say = f"{name} 등록을 지울까요? 저장한 MAC 도 함께 지워져요."
        self._confirm_window(user, say)
        return f"⚠️ {say} {CONFIRM_SECONDS}초 안에 {CONFIRM_ANSWER} 보내면 지워요. {CONFIRM_ELSE}"

    def _rename_computer(self, name, new):
        computers = sysadmin.computers()
        if name not in computers:
            return f"'{name}' 컴퓨터는 없어요.\n{sysadmin.computer_list()}"
        problem = sysadmin.check_name(new)
        if problem:
            return problem
        if new in computers:
            return f"'{new}'은(는) 이미 있어요."
        self.pending = {u: v for u, v in self.pending.items() if v[2] != name}  # 옛 이름으로 기다리던 확인은 취소한다
        sysadmin.rename_computer(name, new)
        return f"{name}의 이름을 {new}(으)로 바꿨어요."

    def _computer_order(self, user, word, others):
        """재우기·깨우기. 이름은 등록한 것만 받고, 모르는 말('이거')이면 실행하지 않고 목록을 보여 준다."""
        sleep = word in sysadmin.SLEEP_WORDS
        if not others and sleep:
            return self._ask_suspend(user)
        name = others[0] if len(others) == 1 else None
        if name not in sysadmin.computers():
            return f"어느 컴퓨터인지 이름으로 말씀해 주세요.\n{sysadmin.computer_list()}"
        if not sysadmin.config.SYSADMIN_CONTROL:
            return sysadmin.CONTROL_OFF
        return self._ask_suspend(user, name) if sleep else self._wake_computer(name)

    def _restart_order(self, user, others):
        """이름이 없으면 이 서버. 등록한 컴퓨터가 아니면 실행하지 않고 목록을 보여 준다."""
        if not others:
            return self._ask_restart(user, RESTART_ASK)
        name = others[0] if len(others) == 1 else None
        if name not in sysadmin.computers():
            return f"어느 컴퓨터인지 이름으로 말씀해 주세요.\n{sysadmin.computer_list()}"
        return self._ask_restart(user, RESTART_ASK, name)

    def _ask_restart(self, user, step, name=None):
        """재시작도 두 번 묻는다. 대답이 없거나 다른 말이면 실행하지 않는다."""
        if not sysadmin.config.SYSADMIN_CONTROL:
            return sysadmin.CONTROL_OFF
        self.pending[user] = (step, time.time() + CONFIRM_SECONDS, name)
        wait = f"{CONFIRM_SECONDS}초 안에 {CONFIRM_ANSWER}"
        if step == RESTART_ASK:
            say = (f"{name}을(를) 강제 재시작합니다. 저장하지 않은 내용은 사라져요." if name
                   else "서버를 재시작합니다. 비서가 다시 켜질 때까지 답을 못 해요.")
            how = f"{wait} 보내면 한 번 더 물어봐요."
        else:
            say = f"정말 {name}을(를) 재시작할까요?" if name else "정말 서버를 재시작할까요?"
            how = f"{wait} 보내면 실행해요."
        self._confirm_window(user, say)
        return f"⚠️ {say} {how} {CONFIRM_ELSE}"

    def _wake_computer(self, name):
        mac = sysadmin.computers()[name][1]
        if not mac:
            return f"{name}의 MAC 을 아직 몰라요. 켜 둔 상태에서 한 번 재우면 저장돼요."
        try:
            sysadmin.wake(mac)
        except OSError as e:
            return f"깨우기 실패: {type(e).__name__}"
        return f"{name}에 깨우기 신호를 보냈어요. 잠시 뒤 '컴퓨터 목록'으로 확인해 주세요."

    def _sleep_computer(self, name):
        mac = sysadmin.read_mac(name)  # 잠들면 읽을 수 없으니 재우기 직전에 저장한다
        if mac:
            sysadmin.set_mac(name, mac)
        return sysadmin.computer_sleep(name)

    def _confirm_window(self, user, message):
        """앱은 확인 창을 띄운다 (규약 5절)."""
        if getattr(self._ctx, "client", False):
            cid = "c_" + files.new_id()[2:]
            self.confirm_ids = {k: v for k, v in self.confirm_ids.items() if v != user}  # 사람마다 하나만
            self.confirm_ids[cid] = user
            self._ctx.confirm = {"type": "confirm", "id": cid, "message": message, "expires_in": CONFIRM_SECONDS}

    def _ask_suspend(self, user, name=None):
        """절전은 한 번 묻는다. name 이 없으면 이 서버. 앱은 확인 창을 띄운다 (규약 5절). 대답이 없거나 다른 말이면 실행하지 않는다."""
        if not sysadmin.config.SYSADMIN_CONTROL:
            return sysadmin.CONTROL_OFF
        self.pending[user] = (SLEEP_CONFIRM, time.time() + CONFIRM_SECONDS, name)
        say = (f"{name}을(를) 절전합니다. 깨우려면 `컴퓨터 {name} 깨워`를 보내 주세요." if name
               else "서버를 절전합니다. 이 봇도 멈추고, 서버 앞에서 직접 깨워야 해요.")
        self._confirm_window(user, say)
        return f"⚠️ {say} {CONFIRM_SECONDS}초 안에 {CONFIRM_ANSWER} 보내면 실행해요. {CONFIRM_ELSE}"

    def _confirm(self, user, rest):
        item = self.pending.pop(user, None)
        if not item:
            return "확인할 대기 작업이 없습니다."
        step, expires, name = item
        if time.time() > expires:
            return "시간이 지나 취소되었습니다. 다시 요청해 주세요."
        if step == DELETE_COMPUTER:
            return f"{name} 등록을 지웠어요." if sysadmin.delete_computer(name) else f"'{name}' 컴퓨터는 없어요."
        if step == RESTART_ASK:
            return self._ask_restart(user, RESTART_LAST, name)
        if step == RESTART_LAST:
            return sysadmin.computer_restart(name) if name else sysadmin.restart_server()
        return self._sleep_computer(name) if name else sysadmin.suspend()

    # --- 모델이 하는 일 ---
    def _checkup(self, user, rest):
        data = sysadmin.snapshot()
        prompt = (
            "아래는 내 서버의 현재 상태다. 한국어로 3~5줄 이내로 요약하고, "
            "디스크/메모리가 80%를 넘거나 이상해 보이는 항목이 있으면 따로 경고해줘. "
            "없는 내용은 지어내지 마.\n\n" + data
        )
        return self._ask([{"role": "user", "content": prompt}])

    def _memo_summary(self, user, rest):
        rows = notes.since(user, 7)
        if not rows:
            return "최근 7일간 메모가 없습니다."
        prompt = "아래는 내 최근 메모다. 핵심을 항목별로 짧게 정리하고, 해야 할 일이 있으면 따로 알려줘.\n\n" + notes.fmt(rows)
        return self._ask([{"role": "user", "content": prompt}])

    def _summary(self, user, rest):
        if not rest:
            return "요약할 글을 같이 보내주세요. 예: 요약 <긴 글>"
        return self._ask([{"role": "user", "content": f"다음 글을 3줄 이내로 요약해줘.\n\n{rest}"}])

    def _chat(self, user, text):
        hist = self.history.setdefault(user, [])
        hist.append({"role": "user", "content": text})
        try:
            answer = self._ask(hist[-MAX_HISTORY:])
        except llm.LLMError:
            hist.pop()
            raise
        hist.append({"role": "assistant", "content": answer})
        del hist[:-MAX_HISTORY]
        return answer
