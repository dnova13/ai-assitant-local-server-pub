"""문서 검색 (README.md). 문서는 Open WebUI 지식 기반에 올려 두고(저장소로만), 비서가 직접 색인·검색·답한다.
- 색인(index): 지식 기반의 파일 글을 가져와 조각으로 나누고 bge-m3(Ollama)로 임베딩해 벡터 저장소(sqlite/qdrant)에 둔다.
- 질문(handle): 질문 임베딩 -> 카테고리 지식 기반 안에서 검색 -> 점수 기준 이상 조각이 없으면 못 찾음(모델 안 부름)
  -> 조각만 근거로 비서 모델이 답 -> 거절 문장이면 못 찾음 -> 답 + 출처.
- 카테고리 표(별칭 -> 이름 -> 지식 기반 ID 목록)는 documents.json (없으면 documents.example.json). 코드에 적지 않는다.
- ~~카테고리별 워크스페이스 모델을 부른다 (15·16절)~~: 2026-10-28 구조 변경. 문서를 올리거나 지우는 동작은 만들지 않는다."""
import hashlib
import json
import re
import time
from pathlib import Path

from core import config
from clients import llm, openwebui_client, vector_store

BASE = Path(__file__).resolve().parent.parent
NOT_FOUND = "관련 문서를 찾지 못했어요."
NOTHING_INDEXED = "아직 색인된 문서가 없어요. !문서 색인 (관리자) 또는 서버에서 python -m scripts.doc_index 를 실행해 주세요."
NOT_IN_DOC = "문서에 없습니다"  # 답 지시문의 거절 문장 (refusals 에 없어도 항상 본다)
JOSA = r"(?:에서|에|의|은|는|을|를|이|가)?"
EMBED_BATCH = 16  # 색인할 때 임베딩 한 번에 보내는 조각 수 (요청은 하나씩 차례로)


class DocumentError(Exception):
    pass


def load_config():
    """documents.json -> dict. 없으면 documents.example.json (카테고리가 비어 있는 기본값)."""
    path = Path(config.DOCUMENTS_CONFIG)
    if not path.exists():
        path = BASE / "documents.example.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DocumentError("문서 검색 설정 파일(documents.json)을 읽지 못했어요.")
    cfg.setdefault("categories", [])
    cfg.setdefault("all_knowledge", [])
    cfg.setdefault("max_sources", 3)
    cfg.setdefault("min_score", 0.0)
    cfg.setdefault("refusals", [])
    return cfg


def is_refusal(answer, refusals):
    """모델이 정해진 거절 문장으로 답했나 (앞부분만 본다: 답 중간의 같은 말은 거절이 아닐 수 있다)."""
    head = answer.strip()[:60]
    return any(r and r in head for r in list(refusals) + [NOT_IN_DOC])


def split_category(cfg, text):
    """'기술 문서 도커 설정' -> (기술 카테고리, '도커 설정'). 맨 앞이 별칭이 아니면 (None, 원문).
    별칭은 공백을 무시하고 비교하고, 뒤에 조사가 붙어도 된다."""
    text = text.strip()
    for c in cfg["categories"]:
        for alias in c.get("aliases") or []:
            letters = r"\s*".join(re.escape(ch) for ch in re.sub(r"\s+", "", alias))
            m = re.match(letters + JOSA + r"(?=\s|$)", text) if letters else None
            if m:
                return c, text[m.end():].strip()
    return None, text


def fmt_sources(sources, category_name, limit):
    files = []
    for s in sources:
        name = s["file"] or s["kb"]
        if name and name not in files:
            files.append(name)
        if len(files) >= limit:
            break
    return f"📎 출처: [{category_name}] " + ", ".join(files) if files else ""


def usage(cfg):
    lines = ["사용법: 문서 <질문>  (예: 문서 디스코드 봇 재시작 설정 찾아줘)",
             "카테고리 안에서만 찾으려면 맨 앞에 별칭: 문서 기술문서 도커 설정",
             "카테고리 보기: 문서 카테고리 / 문서 이름 보기: 문서 목록"]
    for c in cfg["categories"]:
        state = "" if c.get("knowledge") else " (아직 준비 안 됨)"
        lines.append(f"- {'/'.join(c.get('aliases') or [])}: {c.get('name')}{state}")
    return "\n".join(lines)


def all_knowledge(cfg):
    """색인·전체 검색 대상 지식 기반 ID (카테고리 + all_knowledge, 순서 유지, 중복 없음)."""
    ids = []
    for kb in [k for c in cfg["categories"] for k in c.get("knowledge") or []] + list(cfg["all_knowledge"]):
        if kb and kb not in ids:
            ids.append(kb)
    return ids


def chunk(text, size=None, overlap=None):
    """글 -> 조각 목록. 문단(빈 줄)을 모아 size 글자까지 채우고, 긴 문단은 자른다. 이웃 조각은 overlap 글자 겹친다."""
    size = size or config.DOC_CHUNK_SIZE
    overlap = config.DOC_CHUNK_OVERLAP if overlap is None else overlap
    overlap = max(0, min(overlap, size // 2))
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if p.strip()]
    pieces = []
    for p in paras:  # 긴 문단은 size 단위로 자른다
        while len(p) > size:
            pieces.append(p[:size])
            p = p[size - overlap:]
        pieces.append(p)
    chunks, cur = [], ""
    for p in pieces:
        if cur and len(cur) + 2 + len(p) > size:
            chunks.append(cur)
            tail = cur[-overlap:] if overlap else ""
            cur = (tail + "\n\n" + p) if tail and len(tail) + 2 + len(p) <= size else p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    if cur:
        chunks.append(cur)
    return chunks


# --- 문서 전용 분류기 (평소 분류기와 따로. 동작·검색어만 읽는다. 카테고리는 코드가 첫 단어로 정한다, 5_1_4 17.13) ---
DOC_PROMPT = """너는 사용자의 문서 검색 요청을 읽는 도구다. 아래 JSON 한 개만 출력한다. 다른 말은 쓰지 않는다.

{"동작": "검색" 또는 "요약", "검색어": 찾을 말 또는 null}

규칙:
- 검색: 문서에서 무언가를 찾거나 묻는 문장. 요약: 문서 내용을 요약·정리해 달라는 문장.
- 검색어는 문장에 적힌 글자 그대로, "찾아줘", "알려줘", "요약해줘" 같은 요청하는 말은 뺀다.

예시:
문장: 도커 설정 찾아줘
{"동작": "검색", "검색어": "도커 설정"}
문장: 환불 규정 알려줘
{"동작": "검색", "검색어": "환불 규정"}
문장: 계약서 A 요약해줘
{"동작": "요약", "검색어": "계약서 A"}
문장: 내가 한 개인 프로젝트는 뭐야?
{"동작": "검색", "검색어": "내가 한 개인 프로젝트"}"""
ACTIONS = ("검색", "요약")
SUMMARY_WORDS = ("요약", "정리")  # 이 말이 문장에 없으면 모델이 요약이라 해도 검색으로 본다
# 문장 맨 앞이 "문서"(조사가 붙어도 됨)이면 문서 검색을 시도한다 ("문서화가 왜 중요해?"는 아님)
START_PATTERN = r"^\s*문서(?:를|는|은|에서|에|의|가|도|좀)?(?=\s|$)"
NOT_FOUND_PREFIX = "문서에서 찾지 못해 일반 답변으로 드려요."


def starts_with_document(text):
    return bool(re.match(START_PATTERN, text))


def strip_start(text):
    return re.sub(START_PATTERN, "", text, count=1).strip()


def classify(text, model=None, timeout=120):
    """문서 전용 지시문으로 묻고 검증한 결과(parse) 또는 None. 접속 오류는 classifier.ClassifierError."""
    from core import classifier  # classifier 가 features 를 불러오므로 여기서 불러온다
    raw, _ = classifier.ask_system(DOC_PROMPT, text, model, timeout)
    return parse(raw, text)


def parse(raw, text):
    """모델 응답 -> {"동작", "검색어"}. 형식이 틀리면 None.
    검색어는 원문에 있는 글자여야 하고(아니면 None), 요약은 원문에 요약하라는 말이 있어야 한다(아니면 검색)."""
    try:
        d = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("동작") not in ACTIONS:
        return None
    q = d.get("검색어")
    query = q.strip() if isinstance(q, str) and q.strip() and re.sub(r"\s+", "", q) in re.sub(r"\s+", "", text) else None
    action = d["동작"] if any(w in text for w in SUMMARY_WORDS) else "검색"
    return {"동작": action, "검색어": query}


def list_text(cfg):
    lines = ["📚 문서 카테고리"]
    for c in cfg["categories"]:
        state = "" if c.get("knowledge") else " (아직 준비 안 됨)"
        lines.append(f"- {'/'.join(c.get('aliases') or [])}: {c.get('name')}{state}")
    lines.append("카테고리를 말하지 않으면 전체에서 찾아요. 문서 이름은: 문서 목록")
    return "\n".join(lines)


def file_list_text(cfg, store=None):
    """!문서 목록: 색인된 파일 이름 (검색할 수 있는 문서만. Open WebUI 는 부르지 않는다)."""
    kbs = all_knowledge(cfg)
    try:
        names = (store or vector_store.open_store()).file_names(kbs) if kbs else {}
    except vector_store.StoreError as e:
        raise DocumentError(str(e))
    if not names:
        return NOTHING_INDEXED
    groups = [(c.get("name"), c.get("knowledge") or []) for c in cfg["categories"]]
    in_category = {kb for _, kbs_of in groups for kb in kbs_of}
    groups.append(("기타", [kb for kb in kbs if kb not in in_category]))
    lines = [f"📄 색인된 문서 ({sum(len(v) for v in names.values())}개)"]
    for name, kbs_of in groups:
        files = sorted({f for kb in kbs_of for f in names.get(kb, [])})
        if files:
            lines.append(f"[{name}] " + ", ".join(files))
    return "\n".join(lines)


# --- 답 지시문: 찾은 조각만 근거로, 없으면 거절 문장 ---
ANSWER_PROMPT = f"""너는 사용자의 개인 문서에서 답을 찾아 주는 비서다. 항상 한국어로 답한다.
아래 [문서 조각]에 적힌 내용만 근거로 [질문]에 답한다. 조각에 없는 내용은 지어내지 않고 일반 지식으로 채우지 않는다.
먼저 조각에 질문의 답이 직접 적혀 있는지 본다. 질문과 주제가 다르거나 답이 될 문장이 없으면 조각 내용을 말하지 말고, 다른 말 없이 정확히 "{NOT_IN_DOC}."라고만 답한다.
예: 질문이 "오늘 기분이 어때?"이고 조각에 설정 값만 있으면 "{NOT_IN_DOC}."
값이 "A → B"로 적혀 있으면 A는 이전 값, B는 바꾼 뒤의 지금 값이다. 바꾼 값이나 지금 값을 물으면 B로 답한다.
답은 3~5문장으로 짧게 쓴다. 설정 값·명령어·숫자는 조각에 적힌 그대로 옮긴다."""


def build_question(question, hits):
    parts = [f"({i}) 파일: {h['file'] or '이름 없음'}\n{h['text']}" for i, h in enumerate(hits, 1)]
    return "[문서 조각]\n" + "\n\n".join(parts) + f"\n\n[질문]\n{question}"


def default_answer(system, question):
    return llm.chat([{"role": "user", "content": question}], system=system, max_tokens=config.DOC_MAX_TOKENS)


def handle(text, classify_fn=None, answer=None, embed=None, store=None):
    """문서 요청 하나 -> {"kind": "found"|"not_found"|"info", "found": bool, "text": 글}.
    카테고리는 코드가 첫 단어로 정한다 (없으면 전체 검색). classify_fn(text) 가 있으면 동작·검색어를 읽고, 없거나 실패하면 검색.
    answer(system, question) -> 답 글 (비서 모델). embed(texts) -> 벡터 목록. store: 벡터 저장소.
    not_found 는 근거를 못 찾은 경우만 (입구가 일반 대화로 넘길지 정한다). 연결·설정 오류는 DocumentError."""
    answer = answer or default_answer
    embed = embed or llm.embed
    cfg = load_config()
    if not text.strip():
        return _info(usage(cfg))
    if text.strip() == "카테고리":  # 모델에 묻지 않는다 (목록을 질문과 헷갈린다, 5_1_4 17.12)
        return _info(list_text(cfg))
    if text.strip() == "목록":
        return _info(file_list_text(cfg, store))
    category, question = split_category(cfg, text)
    action = "검색"
    r = classify_fn(question) if classify_fn and question else None  # 모델은 별칭을 뺀 글만 본다
    if r:
        action = r["동작"]
    if category is not None and not category.get("knowledge"):
        return _info(f"'{category['name']}' 카테고리는 아직 준비되지 않았어요. 전체에서 찾으려면: 문서 {question}")
    kbs = list(category["knowledge"]) if category else all_knowledge(cfg)
    if not kbs:
        raise DocumentError("문서 검색 설정에 문제가 있어요. (documents.json 에 지식 기반 ID가 없음)")
    if not question:
        return _info("무엇을 찾을지 적어 주세요. 예: 문서 기술문서 도커 설정")
    target = question
    if action == "요약":
        target = (r or {}).get("검색어") or question
        question = f"{target} 내용을 문서에 있는 것만으로 짧게 요약해 줘."
    try:
        store = store or vector_store.open_store()
        if store.count(kbs) == 0:
            return _info(NOTHING_INDEXED)
        if store.other_model(config.EMBED_MODEL):
            raise DocumentError("색인할 때와 임베딩 모델이 달라요. 다시 색인해 주세요. (python -m scripts.doc_index --rebuild)")
        hits = store.search(embed([target])[0], kbs, config.DOC_TOP_K)
    except vector_store.StoreError as e:
        raise DocumentError(str(e))
    except llm.LLMError as e:
        raise DocumentError(str(e))
    # 기준 점수를 넘는 조각이 없으면 모델을 부르지 않고 못 찾음
    good = [h for h in hits if h["score"] >= cfg["min_score"]]
    if not good:
        return {"kind": "not_found", "found": False, "text": NOT_FOUND}
    reply = answer(ANSWER_PROMPT, build_question(question, good)).strip()
    if not reply or is_refusal(reply, cfg["refusals"]):
        return {"kind": "not_found", "found": False, "text": NOT_FOUND}
    src = fmt_sources(good, category["name"] if category else "전체", cfg["max_sources"])
    return {"kind": "found", "found": True, "text": reply + (f"\n\n{src}" if src else "")}


def _info(text):
    return {"kind": "info", "found": False, "text": text}


def search(text, answer=None, embed=None, store=None):
    """분류기 없이 코드 기본 경로로 찾는다 (handle 의 간단한 형태)."""
    return handle(text, None, answer, embed, store)


# --- 색인: 지식 기반 -> 조각 -> 임베딩 -> 저장소 (scripts/doc_index.py 가 부른다) ---
def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def index(cfg, store, list_files=None, get_text=None, embed=None, rebuild=False, report=print):
    """documents.json 의 지식 기반을 저장소에 맞춘다. 글이 같은 파일은 건너뛰고, 사라진 파일은 지운다.
    임베딩 요청은 하나씩 차례로 보내고, 오류(DocumentError)가 나면 그 자리에서 멈춘다.
    -> {"added": 새로 만들거나 바꾼 파일 수, "skipped", "deleted", "chunks": 만든 조각 수}"""
    list_files = list_files or openwebui_client.knowledge_files
    get_text = get_text or openwebui_client.file_text
    embed = embed or llm.embed
    model = config.EMBED_MODEL
    try:
        rebuild = rebuild or store.other_model(model)  # 임베딩 모델이 바뀌었으면 전부 다시
        stats = {"added": 0, "skipped": 0, "deleted": 0, "chunks": 0}
        for kb in all_knowledge(cfg):
            files = list_files(kb)
            old = store.files(kb)
            report(f"[{kb}] 파일 {len(files)}개")
            for f in files:
                text = get_text(f["id"])
                h = text_hash(text)
                if not rebuild and old.get(f["id"]) == h:
                    stats["skipped"] += 1
                    continue
                pieces = chunk(text)
                vectors = []
                for i in range(0, len(pieces), EMBED_BATCH):
                    vectors += embed(pieces[i:i + EMBED_BATCH])
                store.replace_file(kb, f["id"], f["name"], h, model, pieces, vectors)
                stats["added"] += 1
                stats["chunks"] += len(pieces)
                report(f"  색인 {f['name']}: 조각 {len(pieces)}개" + ("" if pieces else " (글이 없음: 스캔본 PDF?)"))
            for file_id in set(old) - {f["id"] for f in files}:
                store.delete_file(kb, file_id)
                stats["deleted"] += 1
                report(f"  지움 {file_id} (지식 기반에서 사라짐)")
        return stats
    except openwebui_client.OpenWebUIError as e:
        raise DocumentError(str(e))
    except (vector_store.StoreError, llm.LLMError) as e:
        raise DocumentError(str(e))


def index_now(store=None, list_files=None, get_text=None, embed=None):
    """!문서 색인: index 를 돌리고 결과를 한 줄로 돌려준다. 오류는 DocumentError (끝난 파일은 저장소에 남는다)."""
    cfg = load_config()
    if not all_knowledge(cfg):
        return "색인할 지식 기반이 없어요. documents.json 의 knowledge 에 ID를 적어 주세요."
    started = time.time()
    try:
        store = store or vector_store.open_store()
    except vector_store.StoreError as e:
        raise DocumentError(str(e))
    stats = index(cfg, store, list_files, get_text, embed, report=lambda line: None)
    return (f"색인 완료 {time.time() - started:.0f}초: 새로 {stats['added']}개(조각 {stats['chunks']}개), "
            f"그대로 {stats['skipped']}개, 지움 {stats['deleted']}개")
