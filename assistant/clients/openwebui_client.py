"""Open WebUI 호출 (문서 색인용, README.md). 지식 기반은 문서 저장소로만 쓴다.
- 지식 기반 목록·파일 목록·파일의 추출된 글만 읽는다. 생성 요청이 아니라 GPU를 쓰지 않는다.
- 응답 모양은 2026-10-28 서버 버전에서 확인했다 (목록 file_count, 파일 목록은 /knowledge/{id}/files). 옛 모양도 받는다.
- ~~워크스페이스 모델에 묻는 ask()~~ 는 2026-10-28 구조 변경으로 없앴다 (답은 비서 모델이 만든다).
- 경로·필드는 Open WebUI 버전에 따라 다를 수 있다. 없는 필드는 오류로 죽지 않고 빈 값으로 둔다.
- 오류 문구와 로그에 주소·키를 넣지 않는다."""
import logging

import requests

from core import config

log = logging.getLogger("assistant.openwebui")


class OpenWebUIError(Exception):
    """kind: 'connect'(연결 실패·시간 초과) / 'auth'(키 문제) / 'format'(응답을 읽을 수 없음, 없는 ID)"""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _get(path, timeout=None):
    if not config.OPENWEBUI_API_KEY:
        raise OpenWebUIError("auth", "문서 색인 설정에 문제가 있어요. (.env 의 OPENWEBUI_API_KEY 가 비어 있음)")
    try:
        r = requests.get(f"{config.OPENWEBUI_URL}{path}",
                         headers={"Authorization": f"Bearer {config.OPENWEBUI_API_KEY}"},
                         timeout=timeout or config.OPENWEBUI_TIMEOUT)
    except UnicodeEncodeError:
        raise OpenWebUIError("auth", "문서 색인 설정에 문제가 있어요. (API 키에 영문·숫자가 아닌 글자)")
    except requests.RequestException as e:
        log.warning("Open WebUI 연결 실패: %s", type(e).__name__)
        raise OpenWebUIError("connect", "Open WebUI 에 연결할 수 없어요.")
    if r.status_code in (401, 403):
        log.warning("Open WebUI 인증 실패: %s", r.status_code)
        raise OpenWebUIError("auth", "문서 색인 설정에 문제가 있어요. (Open WebUI API 키)")
    if r.status_code == 404:
        raise OpenWebUIError("format", "Open WebUI 에서 찾지 못했어요. (지식 기반 ID 또는 API 경로)")
    if not r.ok:
        log.warning("Open WebUI 오류: %s", r.status_code)
        raise OpenWebUIError("connect", f"Open WebUI 오류 ({r.status_code})")
    try:
        return r.json()
    except ValueError:
        raise OpenWebUIError("format", "Open WebUI 응답을 읽지 못했어요.")


def _items(data):
    """목록 응답은 버전에 따라 [..] 또는 {"items": [..]} 모양이다."""
    if isinstance(data, dict):
        data = data.get("items") or data.get("data") or []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def knowledge_list():
    """-> [{"id", "name", "files": 파일 수}]. 새 버전은 file_count, 옛 버전은 files 배열."""
    out = []
    for k in _items(_get("/api/v1/knowledge/")):
        count = k.get("file_count")
        if not isinstance(count, int):
            count = len(k.get("files") or (k.get("data") or {}).get("file_ids") or [])
        out.append({"id": str(k.get("id") or ""), "name": str(k.get("name") or ""), "files": count})
    return out


def _file(f):
    meta = f.get("meta") if isinstance(f.get("meta"), dict) else {}
    return {"id": str(f["id"]), "name": str(meta.get("name") or f.get("filename") or f["id"])}


MAX_PAGES = 100  # 파일 목록 페이지 상한 (응답이 이상해도 끝없이 돌지 않게)


def knowledge_files(kb_id):
    """지식 기반 하나의 파일 -> [{"id", "name"}].
    새 버전: GET /api/v1/knowledge/{id}/files (items, total, 페이지 단위. 상세의 files 는 null, 2026-10-28 확인).
    그 경로가 없으면(404) 옛 버전: 상세 GET /api/v1/knowledge/{id} 의 files."""
    try:
        first = _get(f"/api/v1/knowledge/{kb_id}/files?page=1")
    except OpenWebUIError as e:
        if e.kind != "format":
            raise
        first = None
    if isinstance(first, dict) and isinstance(first.get("items"), list):
        files, page, data = [], 1, first
        while True:
            items = _items(data)
            files += [_file(f) for f in items if f.get("id")]
            total = data.get("total")
            if not items or not isinstance(total, int) or len(files) >= total or page >= MAX_PAGES:
                break
            page += 1
            data = _get(f"/api/v1/knowledge/{kb_id}/files?page={page}")
        seen = set()
        return [f for f in files if not (f["id"] in seen or seen.add(f["id"]))]
    data = _get(f"/api/v1/knowledge/{kb_id}")
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        raise OpenWebUIError("format", "지식 기반의 파일 목록을 읽지 못했어요.")
    return [_file(f) for f in files if isinstance(f, dict) and f.get("id")]


def file_text(file_id):
    """파일에서 Open WebUI 가 추출해 둔 글 (PDF 등도 글로). 없으면 빈 글."""
    data = _get(f"/api/v1/files/{file_id}/data/content")
    text = data.get("content") if isinstance(data, dict) else None
    return text if isinstance(text, str) else ""
