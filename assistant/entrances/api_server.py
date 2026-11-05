"""앱·봇용 입구 (FastAPI). 앱을 만들고 주소 세 묶음을 붙인다.
실행 (assistant/ 폴더): python -m entrances.api_server   또는 ./run.sh api   (포트: .env 의 API_PORT, 기본 8050)

  entrances/api/ollama_compat.py  /api/…       Ollama 호환 (앱이 주소와 키만 바꿔 접속, 대화만)
  entrances/api/app_v1.py         /v1/…        앱 규약 (파일 동작 client_action, 절전 confirm). 앱 키만
  entrances/api/internal.py       /internal/…  봇 전용 (bot 키 + 서버 안에서만)
  entrances/api/common.py         비서 본체 하나, 키 확인, 본문 읽기
키: 요청 헤더 Authorization: Bearer <키>  (또는 X-API-Key: <키>). 키는 python -m core.apikeys 로 발급한다.

- 서버 조회·제어 명령(!상태, !서비스 등)은 키 주인이 관리자일 때만 실행한다.
"""
import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from core import config, users
from entrances.api import app_v1, internal, ollama_compat

ERROR_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict", 410: "expired",
               502: "upstream", 503: "unavailable"}

app = FastAPI(title="local-ai assistant", docs_url=None, redoc_url=None, openapi_url=None)
users.bootstrap()  # 사람이 아무도 없으면 owner(관리자)를 만든다. 이미 있으면 아무것도 안 함

app.include_router(ollama_compat.router)
app.include_router(app_v1.router)
app.include_router(internal.router)


@app.exception_handler(HTTPException)
def _error(request, exc):
    if request.url.path.startswith("/v1/"):  # 앱 규약 형식
        body = {"error": {"code": ERROR_CODES.get(exc.status_code, "error"), "message": exc.detail}}
        return JSONResponse(body, status_code=exc.status_code)
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)  # Ollama 와 같은 오류 형식


class QuietDuePolling(logging.Filter):
    """봇이 10초마다 부르는 /internal/due 의 성공 줄은 접속 로그에 남기지 않는다. 오류는 남긴다."""

    def filter(self, record):
        args = record.args
        return not (isinstance(args, tuple) and len(args) >= 5 and args[2] == "/internal/due" and args[4] == 200)


def main():
    import uvicorn
    logging.getLogger("uvicorn.access").addFilter(QuietDuePolling())
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT, log_level=config.LOG_LEVEL.lower())


if __name__ == "__main__":
    main()
