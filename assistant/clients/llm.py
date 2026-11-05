"""Ollama 호출. 도구/함수 호출은 쓰지 않고 순수 대화만 사용한다."""
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from core import config

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


class LLMError(Exception):
    pass


AUTH_ERROR = "Ollama 인증에 실패했어요. .env 의 OLLAMA_API_KEY 를 확인하세요."  # 키·주소는 문구에 넣지 않는다


def now():
    return datetime.now(ZoneInfo(config.TIMEZONE))


def date_text():
    n = now()
    return f"{n:%Y년 %m월 %d일} {WEEKDAYS[n.weekday()]}요일 {n:%H:%M}"


def system_prompt():
    return (
        "너는 사용자의 개인 비서다. 항상 한국어로 간결하게 답한다. "
        "도구 호출이나 JSON 형식은 쓰지 않고 일반 문장으로만 답한다. "
        "너에게는 날씨, 메모, 서버 점검 기능이 있지만 인터넷 검색은 할 수 없다. "
        "앱, 서비스, 상품, 인물, 가게, 최신 소식처럼 확실한 사실이 필요한 질문에는 "
        "이름이나 정보를 지어내지 않는다. 확실히 아는 것만 말하고, 모르면 "
        "'정확히 알려 드리기 어려워요. 검색 기능이 없어서 직접 찾아보시는 게 좋아요'라고 답한다. "
        "개념 설명, 요약, 번역, 코드, 글 다듬기처럼 아는 내용으로 답할 수 있는 질문은 평소처럼 답한다. "
        f"현재 시각은 {date_text()} (한국 시간)이다."
    )


def chat(messages, model=None, timeout=300, system=None, max_tokens=None):
    """system 이 있으면 비서 기본 지시문 대신 쓴다 (문서 답처럼 따로 지시할 때). max_tokens 는 답 길이 한도."""
    options = {"num_ctx": 4096}
    if max_tokens:
        options["num_predict"] = max_tokens
    payload = {
        "model": model or config.MODEL,
        "messages": [{"role": "system", "content": system or system_prompt()}] + messages,
        "stream": False,
        "options": options,
    }
    try:
        r = requests.post(f"{config.OLLAMA_URL}/api/chat", json=payload, headers=config.ollama_headers(), timeout=timeout)
        r.raise_for_status()
    except requests.ConnectionError:
        raise LLMError("Ollama에 연결할 수 없습니다. 서버와 .env 의 OLLAMA_URL 을 확인하세요.")  # 주소는 앱 사용자에게 보이지 않게
    except requests.Timeout:
        raise LLMError("응답 시간이 초과되었습니다. 모델이 로딩 중일 수 있으니 잠시 후 다시 시도하세요.")
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", "?")
        if status in (401, 403):
            raise LLMError(AUTH_ERROR)
        raise LLMError(f"Ollama 오류 ({status}) (모델 이름이 맞는지 `ollama list`로 확인)")
    return r.json()["message"]["content"].strip()


def embed(texts, model=None, timeout=120):
    """글 목록 -> 벡터 목록 (Ollama /api/embed, 기본 EMBED_MODEL). 오류는 LLMError (주소는 문구에 넣지 않는다)."""
    try:
        r = requests.post(f"{config.OLLAMA_URL}/api/embed",
                          json={"model": model or config.EMBED_MODEL, "input": list(texts)},
                          headers=config.ollama_headers(), timeout=timeout)
        r.raise_for_status()
        vectors = r.json()["embeddings"]
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", "?")
        if status in (401, 403):
            raise LLMError(AUTH_ERROR)
        raise LLMError(f"임베딩 오류 ({status}) (임베딩 모델이 받아져 있는지 `ollama list`로 확인)")
    except requests.RequestException:
        raise LLMError("문서 검색용 임베딩 모델에 연결할 수 없어요.")
    except (ValueError, KeyError, TypeError):
        raise LLMError("임베딩 응답을 읽지 못했어요.")
    if len(vectors) != len(texts):
        raise LLMError("임베딩 응답을 읽지 못했어요.")
    return vectors
