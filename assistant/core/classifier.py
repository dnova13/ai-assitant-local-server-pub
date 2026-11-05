"""분류기: 자유 문장을 모델이 읽고 {"의도", "지역", "날짜", "내용"} 로 분류한다 (비서 assistant.py 가 부른다).
모델은 문장에 적힌 글자만 뽑고, 판정·값 조회·실행은 코드가 한다.
- 날씨: 날짜가 며칠인지(또는 날짜로 바꿀 수 없는지)는 코드(date_from_phrase)가 정하고, 날씨 값과 좌표도 코드가 가져온다.
- 메모: 저장할 내용은 문장에 실제로 있는 글자여야 하고(코드가 검사), 삭제 번호는 코드가 숫자로 바꾼다. 일정 삭제 번호도 같다.
- 일정: 등록할 글자(날짜·시각·할 일)는 문장에 실제로 있는 글자여야 하고, 날짜·시각 해석과 등록 확인은 코드가 한다 (features/schedule.py).
시험: python -m scripts.classifier_eval   (서버 Ollama가 필요하다. OLLAMA_URL 은 config.py / .env 를 따른다)
"""
import json
import re
import time

import requests

from core import config
from features import weather

INTENTS = ("날씨", "메모저장", "메모조회", "메모삭제", "메모요약", "일정등록", "일정조회", "일정삭제", "일반대화")
MEMO_INTENTS = ("메모저장", "메모조회", "메모삭제", "메모요약")
SCHEDULE_INTENTS = ("일정등록", "일정조회", "일정삭제")
DELETE_INTENTS = ("메모삭제", "일정삭제")
UNSUPPORTED_DATE_WORDS = weather.UNSUPPORTED_DATE_WORDS
# 분류에 실패했을 때 날씨 질문이었을 수 있는지 보는 말 (이런 문장은 일반대화로 넘기지 않고 다시 물어본다)
WEATHER_WORDS = ("날씨", "비 ", "비와", "비 와", "비가", "눈 ", "눈와", "눈이", "기온", "온도", "우산", "추워", "추운", "춥",
                 "더워", "더운", "덥", "바람", "미세먼지", "태풍", "소나기")
# 분류에 실패했을 때 메모 요청이었을 수 있는지 보는 말
MEMO_WORDS = ("메모", "적어", "기록", "저장해", "기억해")
# 분류에 실패했을 때 일정 요청이었을 수 있는지 보는 말
SCHEDULE_WORDS = ("일정", "스케줄", "알림", "알려", "약속 잡")
# 모델이 일정등록으로 읽었어도 시각이 없고 이런 요청 말도 없으면 일반대화로 본다 ("제주도 여행 3일 코스 짜줘")
SCHEDULE_REQUEST_WORDS = ("일정", "스케줄", "등록", "잡아", "알림", "알려", "추가", "넣어", "예약")
THINK = None  # 시험 스크립트의 --think 값. None 이면 보내지 않는다 (Ollama 기본값, 비서는 항상 None)
MAX_MEMO_LEN = 500   # 저장할 메모 글자 수 한도
MAX_QUERY_LEN = 30   # 메모 검색어 글자 수 한도
MAX_REGION_LEN = 20  # 지역 글자가 이보다 길면 문장이 통째로 들어온 것으로 보고 형식 오류로 처리

SYSTEM_PROMPT = """너는 사용자의 문장을 분류하는 도구다. 문장을 읽고 아래 JSON 한 개만 출력한다. 다른 말은 쓰지 않는다.

{"의도": "날씨" 또는 "메모저장" 또는 "메모조회" 또는 "메모삭제" 또는 "메모요약" 또는 "일정등록" 또는 "일정조회" 또는 "일정삭제" 또는 "일반대화", "지역": 문장에 적힌 지역 이름 또는 null, "날짜": 문장에 적힌 날짜 표현 또는 null, "내용": 아래 규칙의 글자 또는 null}

규칙:
- 의도가 "날씨"인 경우: 지금 또는 앞으로의 날씨(비, 눈, 기온, 추위, 더위, 바람, 우산 필요 여부 등)를 묻는 문장이다. '날씨'라는 단어가 없어도 뜻이 그렇다면 날씨다.
- 의도가 "메모저장"인 경우: 사용자가 무언가를 메모해 두라, 적어 두라, 기억해 두라고 요청하는 문장이다.
- 의도가 "메모조회"인 경우: 저장해 둔 메모를 보여 주거나 찾아 달라는 문장이다.
- 의도가 "메모삭제"인 경우: 저장해 둔 메모를 지워 달라는 문장이다. 문장에 '메모'가 있다.
- 의도가 "메모요약"인 경우: 저장해 둔 메모를 요약하거나 정리해 달라는 문장이다.
- 의도가 "일정등록"인 경우: 정해진 날짜·시각의 일정이나 알림을 등록하거나 잡아 달라고, 또는 그 시각이나 'N분 뒤', 'N시간 후'에 알려 달라고 요청하는 문장이다. '메모해줘', '적어 둬', '기억해'라고 하면 일정이 아니라 메모저장이다.
- 의도가 "일정조회"인 경우: 등록한 일정이나 알림을 보여 달라거나 앞으로 무슨 일정이 있는지 묻는 문장이다.
- 의도가 "일정삭제"인 경우: 등록한 일정, 알림, 약속을 지워 달라는 문장이다. 문장에 '일정', '알림', '약속' 중 하나가 있다.
- 의도가 "일반대화"인 경우: 그 밖의 모든 문장이다. '비', '날씨', '메모' 같은 단어가 들어 있어도 노래, 영화, 요리, 번역, 설명, 코드, 앱 추천, 방법, 조언을 요청하는 문장이면 일반대화다.
- 지역: 문장에 적힌 지역 이름을 그대로 쓴다. 도·광역시 이름이 앞에 붙어 있으면 함께 쓴다(예: 경기 수원, 충남 천안). 문장에 지역이 없으면 null. 지역을 추측하지 않는다.
- 날짜: 문장에 적힌 날짜 표현을 그대로 쓴다(예: 오늘, 내일, 모레, 10월 23일, 23일, 이번 주말). 날짜 표현이 없으면 null. 날짜를 추측하거나 바꾸지 않는다.
- 내용: 메모저장이면 "메모해줘", "적어 줘", "기억해" 같은 요청하는 말만 빼고 사용자가 쓴 글자를 그대로 쓴다. 바꾸거나 요약하거나 덧붙이지 않는다. 메모조회이고 찾을 단어가 있으면 그 단어만 쓰고 없으면 null. 메모삭제와 일정삭제이면 지울 번호(숫자만), 번호가 없으면 null. 일정등록이면 "일정 잡아줘", "알림 잡아줘", "등록해줘", "알려줘" 같은 요청하는 말만 빼고 날짜·시각·할 일을 사용자가 쓴 글자 그대로 쓴다. 그 밖의 의도는 null.
- 메모·일정 의도와 일반대화이면 지역과 날짜는 null이다 (메모·일정 내용에 든 날짜와 지역은 내용에 그대로 둔다).

예시:
문장: 대구 오늘 눈 오나?
{"의도": "날씨", "지역": "대구", "날짜": "오늘", "내용": null}
문장: 인천 내일 쌀쌀해?
{"의도": "날씨", "지역": "인천", "날짜": "내일", "내용": null}
문장: 울산 날씨 알려줘
{"의도": "날씨", "지역": "울산", "날짜": null, "내용": null}
문장: 우산 챙겨야 해?
{"의도": "날씨", "지역": null, "날짜": null, "내용": null}
문장: 경기 수원 내일 비 와?
{"의도": "날씨", "지역": "경기 수원", "날짜": "내일", "내용": null}
문장: 충남 천안 날씨 어때?
{"의도": "날씨", "지역": "충남 천안", "날짜": null, "내용": null}
문장: 이번 주말 대전 날씨 알려줘
{"의도": "날씨", "지역": "대전", "날짜": "이번 주말", "내용": null}
문장: 모레 인천 비 와?
{"의도": "날씨", "지역": "인천", "날짜": "모레", "내용": null}
문장: 청주 10월 23일 날씨 어때?
{"의도": "날씨", "지역": "청주", "날짜": "10월 23일", "내용": null}
문장: 금요일 저녁 약속 있다고 메모해 줘
{"의도": "메모저장", "지역": null, "날짜": null, "내용": "금요일 저녁 약속 있다"}
문장: 빨래 널기 적어 둬
{"의도": "메모저장", "지역": null, "날짜": null, "내용": "빨래 널기"}
문장: 광주 출장 11월 5일 기억해 줘
{"의도": "메모저장", "지역": null, "날짜": null, "내용": "광주 출장 11월 5일"}
문장: 저장한 메모 다 보여줘
{"의도": "메모조회", "지역": null, "날짜": null, "내용": null}
문장: 약속 들어간 메모 있어?
{"의도": "메모조회", "지역": null, "날짜": null, "내용": "약속"}
문장: 7번 메모 삭제해 줘
{"의도": "메모삭제", "지역": null, "날짜": null, "내용": "7"}
문장: 아까 저장한 메모 지워 줘
{"의도": "메모삭제", "지역": null, "날짜": null, "내용": null}
문장: 지난주에 적은 메모 요약해 줘
{"의도": "메모요약", "지역": null, "날짜": null, "내용": null}
문장: 내일 오후 2시 병원 일정 잡아 줘
{"의도": "일정등록", "지역": null, "날짜": null, "내용": "내일 오후 2시 병원"}
문장: 모레 10시에 회의 있다고 알려 줘
{"의도": "일정등록", "지역": null, "날짜": null, "내용": "모레 10시에 회의 있다고"}
문장: 11월 2일 7시 동창회 일정 등록해 줘
{"의도": "일정등록", "지역": null, "날짜": null, "내용": "11월 2일 7시 동창회"}
문장: 20분 뒤에 빨래 널기 알려 줘
{"의도": "일정등록", "지역": null, "날짜": null, "내용": "20분 뒤에 빨래 널기"}
문장: 2시간 후 회의 준비 알림 잡아 줘
{"의도": "일정등록", "지역": null, "날짜": null, "내용": "2시간 후 회의 준비"}
문장: 이번 주 금요일 15시 치과 일정 잡아 줘
{"의도": "일정등록", "지역": null, "날짜": null, "내용": "이번 주 금요일 15시 치과"}
문장: 내 일정 뭐 있어?
{"의도": "일정조회", "지역": null, "날짜": null, "내용": null}
문장: 내 알림 뭐 있어?
{"의도": "일정조회", "지역": null, "날짜": null, "내용": null}
문장: 다가오는 약속 목록 보여 줘
{"의도": "일정조회", "지역": null, "날짜": null, "내용": null}
문장: 5번 일정 지워 줘
{"의도": "일정삭제", "지역": null, "날짜": null, "내용": "5"}
문장: 2번 알림 삭제해 줘
{"의도": "일정삭제", "지역": null, "날짜": null, "내용": "2"}
문장: 약속 4번 없애 줘
{"의도": "일정삭제", "지역": null, "날짜": null, "내용": "4"}
문장: 방금 잡은 일정 지워 줘
{"의도": "일정삭제", "지역": null, "날짜": null, "내용": null}
문장: 장마철에 곰팡이 없애는 법 알려줘
{"의도": "일반대화", "지역": null, "날짜": null, "내용": null}
문장: 비 오는 날 어울리는 영화 추천해줘
{"의도": "일반대화", "지역": null, "날짜": null, "내용": null}
문장: 눈 오는 날 운전할 때 조심할 점은?
{"의도": "일반대화", "지역": null, "날짜": null, "내용": null}
문장: 자바스크립트 배열 합치는 법
{"의도": "일반대화", "지역": null, "날짜": null, "내용": null}
문장: 안녕 뭐 하고 있어?
{"의도": "일반대화", "지역": null, "날짜": null, "내용": null}
문장: 노트 필기 잘하는 요령 알려줘
{"의도": "일반대화", "지역": null, "날짜": null, "내용": null}
문장: 스마트폰 메모장 앱 비교해줘
{"의도": "일반대화", "지역": null, "날짜": null, "내용": null}
문장: 일정 관리 잘하는 방법 알려줘
{"의도": "일반대화", "지역": null, "날짜": null, "내용": null}"""


class ClassifierError(Exception):
    pass


def set_think(option):
    """시험 스크립트의 --think on|off 를 적용한다 (생각 기능이 있는 모델을 공정하게 비교할 때)."""
    global THINK
    THINK = {"on": True, "off": False}.get(option)


def ask_model(text, model=None, timeout=120):
    """모델에게 문장을 보내 (원문 응답, 걸린 시간(초))을 돌려준다. 접속·응답 오류는 ClassifierError."""
    return ask_system(SYSTEM_PROMPT, text, model, timeout)


def ask_system(system, text, model=None, timeout=120):
    """지시문(system)을 정해 묻는다 (파일 분류는 features/files.py 의 지시문을 쓴다)."""
    body = {
        "model": model or config.MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": f"문장: {text}"}],
        "stream": False,
        "format": "json",            # JSON만 내도록 강제 [확인 필요: 서버 Ollama 버전이 지원하는지]
        "options": {"temperature": 0},
    }
    if THINK is not None:
        body["think"] = THINK
    t0 = time.time()
    try:
        r = requests.post(f"{config.OLLAMA_URL}/api/chat", json=body, headers=config.ollama_headers(), timeout=timeout)
        r.raise_for_status()
        raw = r.json()["message"]["content"]
    except requests.RequestException as e:
        # 주소가 들어간 원문(e)은 앱 사용자에게 보이지 않게 종류와 상태 코드만 알린다
        status = getattr(getattr(e, "response", None), "status_code", None)
        hint = {404: " (모델 이름이 맞는지 확인)", 401: " (.env 의 OLLAMA_API_KEY 확인)",
                403: " (.env 의 OLLAMA_API_KEY 확인)"}.get(status, "")
        raise ClassifierError(f"Ollama 호출 오류 ({type(e).__name__}{f' {status}' if status else ''}){hint}")
    except (KeyError, ValueError):
        raise ClassifierError("Ollama 응답 형식이 예상과 다릅니다.")
    return raw, time.time() - t0


def date_from_phrase(phrase, now=None):
    """모델이 뽑은 날짜 글자를 코드가 판정한다 (weather.normalize_day).
    '어제'/'오늘'/'내일'/'모레' 또는 'YYYY-MM-DD', 날짜로 바꿀 수 없으면 None. 날짜 말이 없으면(null) 오늘."""
    return weather.normalize_day(phrase, now)


def _squash(t):
    return re.sub(r"\s+", "", t or "")


def _weather_phrase(phrase, text):
    """날씨 날짜 글자: 모델이 원문에 없는 날짜를 말했거나 놓쳤으면 원문의 날짜 말을 쓴다 ('낼'을 오늘로 읽는 경우).
    날짜로 바꿀 수 없는 말(주말, 다음 주)이 있으면 그대로 둔다.
    '3일 뒤'를 놓쳤거나 '3일'만 뽑았으면 그 말을 써서 오늘이나 그달 3일 날씨로 답하지 않는다."""
    days = re.search(weather.RELATIVE_DAYS_PATTERN, text) if text else None
    if days and (not phrase or _squash(phrase) in _squash(days.group(0)) or _squash(phrase) not in _squash(text)):
        return days.group(0)
    if text is None or (phrase and _squash(phrase) in _squash(text)):
        return phrase
    if any(w in text for w in weather.UNSUPPORTED_DATE_WORDS):
        return phrase
    m = re.search(weather.DATE_PATTERN, text)
    return m.group(0).strip() if m else phrase


def _keep_schedule_date(content, text):
    """일정 내용: 모델이 원문의 날짜를 빼먹었으면 앞에 붙인다 (빼먹으면 오늘로 등록된다). '10분 뒤'처럼 상대 시간이면 그대로."""
    from features import schedule  # schedule 이 weather 를 쓰므로 여기서 불러온다
    if (text is None or re.search(weather.DATE_PATTERN, content) or re.search(schedule.WEEKDAY_PATTERN, content)
            or schedule.parse_relative(content) is not None):
        return content
    m = re.search(weather.DATE_PATTERN, text) or re.search(schedule.WEEKDAY_PATTERN, text)
    return f"{m.group(0).strip()} {content}" if m else content


def parse(raw, text=None):
    """모델 응답(글자)을 검증해 {"의도", "지역", "날짜", "날짜글자", "내용"} 로 돌려준다. 형식이 틀리면 None.
    날짜는 date_from_phrase 가 정한 값("어제"/"오늘"/"내일"/"모레"/"YYYY-MM-DD"/None=날짜로 바꿀 수 없음)이고, 모델이 낸 글자는 "날짜글자"에 남긴다.
    일정등록으로 읽었어도 원문에 메모 말이 있고 일정을 요청하는 말이 없으면 메모저장으로 고친다 ("엄마 생일 11월 3일 기억해줘").
    메모저장·일정등록의 내용은 비어 있지 않고, text(원문)를 주면 원문에 실제로 들어 있는 글자여야 한다 (모델이 지어낸 내용은 형식 오류).
    메모삭제·일정삭제의 내용은 1 이상의 숫자(없거나, text 를 주었는데 원문에 없는 번호면 None), 메모조회의 내용은 검색어(없으면 None).
    날씨가 아니면 지역·날짜는 None, 일반대화·메모요약·날씨이면 내용은 None 으로 정리한다."""
    try:
        d = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("의도") not in INTENTS:
        return None
    intent = d["의도"]
    if (intent == "일정등록" and text is not None and looks_like_memo(text)
            and not any(w in text for w in SCHEDULE_REQUEST_WORDS)):
        intent = "메모저장"
    base = {"의도": intent, "지역": None, "날짜": None, "날짜글자": None, "내용": None}
    if intent in ("일반대화", "메모요약", "일정조회"):
        return base
    if intent in DELETE_INTENTS:
        content = d.get("내용")
        m = re.fullmatch(r"#?(\d{1,9})번?", str(content).strip()) if isinstance(content, (str, int)) and not isinstance(content, bool) else None
        number = int(m.group(1)) if m and int(m.group(1)) >= 1 else None
        if number is not None and text is not None and number not in map(int, re.findall(r"\d+", text)):
            number = None  # 모델이 지어낸 번호로 지우지 않는다
        base["내용"] = number
        return base
    if intent == "일정등록":
        content = d.get("내용")
        if not isinstance(content, str) or not content.strip() or len(content.strip()) > MAX_MEMO_LEN:
            return None
        if text is not None and _squash(content) not in _squash(text):
            return None
        base["내용"] = _keep_schedule_date(content.strip(), text)
        return base
    if intent in MEMO_INTENTS:
        content = d.get("내용")
        if intent == "메모저장":
            if not isinstance(content, str) or not content.strip() or len(content.strip()) > MAX_MEMO_LEN:
                return None
            content = content.strip()
            if text is not None and _squash(content) not in _squash(text):
                return None
            base["내용"] = content
        elif intent == "메모조회":
            if content is not None and not isinstance(content, str):
                return None
            content = (content or "").strip()
            if len(content) > MAX_QUERY_LEN:
                return None
            base["내용"] = content or None
        return base
    region, phrase = d.get("지역"), d.get("날짜")
    if region is not None:
        if not isinstance(region, str):
            return None
        region = region.strip() or None
        if region and len(region) > MAX_REGION_LEN:
            return None
    if phrase is not None and not isinstance(phrase, str):
        return None
    phrase = _weather_phrase(phrase, text)
    base.update({"지역": region, "날짜": date_from_phrase(phrase), "날짜글자": phrase})
    return base


def classify(text, model=None, timeout=120):
    """(검증된 결과 또는 None, 원문 응답, 걸린 시간)을 돌려준다."""
    raw, secs = ask_model(text, model, timeout)
    return parse(raw, text), raw, secs


def looks_like_weather(text):
    t = f"{text} "
    return any(w in t for w in WEATHER_WORDS)


def looks_like_memo(text):
    return any(w in text for w in MEMO_WORDS)


def is_schedule_request(text, content):
    """일정등록으로 읽힌 문장을 코드가 한 번 더 본다: 내용에 시각이 있거나, 원문에 일정을 요청하는 말이 있어야 한다."""
    from features import schedule
    return schedule.has_time(content) or any(w in text for w in SCHEDULE_REQUEST_WORDS)


def looks_like_schedule(text):
    """일정 말과 시각이 함께 있을 때만 ("여행 일정 짜줘"는 시각이 없어 대화로 보낸다)."""
    from features import schedule  # schedule 이 weather 를 쓰므로 여기서 불러온다
    return schedule.has_time(text) and any(w in text for w in SCHEDULE_WORDS)
