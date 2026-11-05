"""설정. 환경변수 또는 assistant/.env 파일에서 읽는다."""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # assistant/ 폴더 (.env, data/ 위치)


def _load_env():
    p = BASE / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
# Ollama 앞에 인증(nginx 등)이 있으면 그 키. 있으면 모든 Ollama 요청에 Bearer 헤더를 붙인다 (.env 에만)
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")


def ollama_headers():
    """Ollama 요청 헤더 (키가 없으면 빈 dict: 서버 안 localhost 처럼 인증이 없는 경우)."""
    return {"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else {}
MODEL = os.getenv("ASSISTANT_MODEL", "qwen2.5-coder:7b")
DB_PATH = os.getenv("ASSISTANT_DB", str(BASE / "data" / "assistant.db"))
# 비서 DB: sqlite(기본, DB_PATH 파일) 또는 postgres (docker compose, deploy/postgres). 단위 시험은 실제 DB 를 건드리지 않게 항상 sqlite 다
DB_BACKEND = os.getenv("DB_BACKEND", "sqlite").lower()
_program = Path(sys.argv[0] if sys.argv else "")  # 어떤 라이브러리가 unittest 를 불러와도 실제 실행이 sqlite 로 바뀌지 않게 실행한 프로그램을 본다
if (_program.parent.name in ("unittest", "tests") or _program.stem == "pytest") and not os.getenv("TEST_POSTGRES"):
    DB_BACKEND = "sqlite"
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "127.0.0.1")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "assistant")
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")  # .env 에만
POSTGRES_RO_USER = os.getenv("POSTGRES_RO_USER", "assistant_ro")  # !SQL 이 쓰는 읽기 전용 계정
POSTGRES_RO_PASSWORD = os.getenv("POSTGRES_RO_PASSWORD", "")  # .env 에만
TIMEZONE = os.getenv("ASSISTANT_TZ", "Asia/Seoul")

# 날씨 위치 (기본: 서울시청). .env 에서 바꾼다
WEATHER_LAT = float(os.getenv("WEATHER_LAT", "37.5665"))
WEATHER_LON = float(os.getenv("WEATHER_LON", "126.9780"))
WEATHER_PLACE = os.getenv("WEATHER_PLACE", "서울")
# 지역 찾는 방식: table(코드의 표만) / geo(지명 검색만) / both(카카오 -> 오류 시 표 -> Open-Meteo 검색)
# 두 방식을 시험해서 나은 쪽으로 정한다 (README.md)
WEATHER_LOOKUP = os.getenv("WEATHER_LOOKUP", "both").lower()
# 카카오 로컬 API 키 (선택). 있으면 지역 찾기에 카카오를 먼저 쓰고, 오류·한도 초과면 표 -> Open-Meteo 검색 순서로 넘어간다. .env 에만 넣는다
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "")

# 아침 브리핑: 켜는 사람의 기본 시각. 뉴스는 !뉴스 로 옮겼다 (README.md)
BRIEFING_TIME = os.getenv("BRIEFING_TIME", "07:30")

# 문서 검색 (README.md): Open WebUI 지식 기반은 문서 저장소로만 쓴다. 색인할 때 글을 가져오는 데만 주소와 키가 필요 (.env 에만)
OPENWEBUI_URL = os.getenv("OPENWEBUI_URL", "http://localhost:3011").rstrip("/")
OPENWEBUI_API_KEY = os.getenv("OPENWEBUI_API_KEY", "")
OPENWEBUI_TIMEOUT = int(os.getenv("OPENWEBUI_TIMEOUT", "120"))
# 벡터 저장소: sqlite(기본, data/vectors.db) 또는 qdrant (서버 따로, pip install qdrant-client). 바꾸면 다시 색인한다
VECTOR_STORE = os.getenv("VECTOR_STORE", "sqlite").lower()
VECTOR_DB_PATH = os.getenv("VECTOR_DB_PATH", str(BASE / "data" / "vectors.db"))
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333").rstrip("/")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "assistant_docs")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
# 임베딩 모델 (Ollama). 바꾸면 다시 색인한다
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
DOC_CHUNK_SIZE = int(os.getenv("DOC_CHUNK_SIZE", "800"))  # 조각 글자 수
DOC_CHUNK_OVERLAP = int(os.getenv("DOC_CHUNK_OVERLAP", "100"))
DOC_TOP_K = int(os.getenv("DOC_TOP_K", "5"))  # 검색해서 가져올 조각 수
# 문서 답 길이 한도 (Ollama num_predict). 긴 답이 GPU를 오래 잡는 것을 막는다 (5_1_4 16.3)
DOC_MAX_TOKENS = int(os.getenv("DOC_MAX_TOKENS", "400"))
# 카테고리 표·점수 기준 (documents.example.json 을 복사해 documents.json 으로 쓴다)
DOCUMENTS_CONFIG = os.getenv("DOCUMENTS_CONFIG", str(BASE / "documents.json"))

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
ALLOWED_USER_IDS = [
    x for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
]
# 문장으로 대화할 서버(그 안의 모든 채널)와 채널의 ID. 그 밖의 채널은 '!' 명령만 받는다 (비우면 DM에서만 문장을 받는다)
DISCORD_CHAT_GUILDS = [x for x in os.getenv("DISCORD_CHAT_GUILDS", "").replace(" ", "").split(",") if x]
DISCORD_CHAT_CHANNELS = [x for x in os.getenv("DISCORD_CHAT_CHANNELS", "").replace(" ", "").split(",") if x]

# 절전과 깨우기 허용 여부 (기본 꺼짐: 조회만 가능)
SYSADMIN_CONTROL = os.getenv("SYSADMIN_CONTROL", "false").lower() in ("1", "true", "yes")
# 다른 컴퓨터를 재울 때 쓰는 전용 키. 컴퓨터는 비서 DB 에 등록한다 (!컴퓨터 등록, README.md)
COMPUTER_SSH_KEY = os.path.expanduser(os.getenv("COMPUTER_SSH_KEY", "~/.ssh/assistant_sleep"))

# 앱용 입구(entrances/api_server.py). 포트 8050, 받는 주소 0.0.0.0(밖에서도 접속) 또는 127.0.0.1(서버 안에서만)
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8050"))

# 디스코드 봇 같은 서버 안 입구가 비서 입구(API)를 부를 때 쓰는 주소와 봇 키 (키는 python -m core.apikeys create owner "디스코드 봇" --kind bot 으로 발급)
API_URL = os.getenv("API_URL", f"http://127.0.0.1:{API_PORT}").rstrip("/")
BOT_API_KEY = os.getenv("BOT_API_KEY", "")

# 로그 수준: INFO(기본) / DEBUG(연결 문제 추적용)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
