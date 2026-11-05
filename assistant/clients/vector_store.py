"""문서 조각 벡터 저장소 (README.md). .env 의 VECTOR_STORE 로 sqlite(기본) 또는 qdrant 를 고른다.
- 두 저장소는 같은 함수 모양: replace_file / delete_file / files / search / count / other_model.
- 점수는 둘 다 코사인 유사도(높을수록 관련). 저장소끼리 데이터를 공유하지 않으므로 바꾸면 다시 색인한다.
- 조각마다 임베딩 모델 이름을 같이 저장해, 색인 때와 모델이 바뀌었는지 확인한다.
- 조각 하나: {"kb", "file_id", "file", "hash", "model", "idx", "text"} + 벡터."""
import sqlite3
import uuid
from pathlib import Path

import numpy as np

from core import config


class StoreError(Exception):
    pass


def _norm(v):
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else v


def _by_kb(rows):
    """(kb, 파일 이름) 줄들 -> {kb: [중복 없이 이름순]}."""
    out = {}
    for kb, name in rows:
        if name:
            out.setdefault(kb, set()).add(name)
    return {kb: sorted(names) for kb, names in out.items()}


class SqliteStore:
    """SQLite 한 파일에 조각과 벡터(float32)를 둔다. 검색은 numpy로 전부 비교 (조각 수천 개까지 충분히 빠름)."""

    def __init__(self, path=None):
        self.path = path or config.VECTOR_DB_PATH
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS chunks (
            kb TEXT, file_id TEXT, file TEXT, hash TEXT, model TEXT, idx INTEGER, text TEXT, vec BLOB,
            PRIMARY KEY (kb, file_id, idx))""")
        self.db.commit()

    def replace_file(self, kb, file_id, file, hash_, model, texts, vectors):
        with self.db:
            self.db.execute("DELETE FROM chunks WHERE kb=? AND file_id=?", (kb, file_id))
            self.db.executemany(
                "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)",
                [(kb, file_id, file, hash_, model, i, t, _norm(v).tobytes()) for i, (t, v) in enumerate(zip(texts, vectors))])

    def delete_file(self, kb, file_id):
        with self.db:
            self.db.execute("DELETE FROM chunks WHERE kb=? AND file_id=?", (kb, file_id))

    def files(self, kb):
        """{file_id: hash} (색인할 때 바뀐 파일만 고르려고)."""
        return dict(self.db.execute("SELECT DISTINCT file_id, hash FROM chunks WHERE kb=?", (kb,)).fetchall())

    def file_names(self, kbs=None):
        """{kb: [파일 이름]} (!문서 목록)."""
        q, args = self._where(kbs)
        return _by_kb(self.db.execute("SELECT DISTINCT kb, file FROM chunks" + q, args).fetchall())

    def count(self, kbs=None):
        q, args = self._where(kbs)
        return self.db.execute("SELECT COUNT(*) FROM chunks" + q, args).fetchone()[0]

    def other_model(self, model):
        """이 임베딩 모델이 아닌 모델로 만든 조각이 있나 (있으면 다시 색인해야 한다)."""
        return self.db.execute("SELECT 1 FROM chunks WHERE model != ? LIMIT 1", (model,)).fetchone() is not None

    def search(self, vector, kbs=None, k=5):
        """kbs(지식 기반 ID 목록)가 None 이면 전체. -> 점수 높은 순 [{"kb", "file", "score", "text"}]."""
        q, args = self._where(kbs)
        rows = self.db.execute("SELECT kb, file, text, vec FROM chunks" + q, args).fetchall()
        if not rows:
            return []
        mat = np.frombuffer(b"".join(r[3] for r in rows), dtype=np.float32).reshape(len(rows), -1)
        scores = mat @ _norm(vector)
        top = np.argsort(-scores)[:k]
        return [{"kb": rows[i][0], "file": rows[i][1], "score": float(scores[i]), "text": rows[i][2]} for i in top]

    @staticmethod
    def _where(kbs):
        if kbs is None:
            return "", ()
        return f" WHERE kb IN ({','.join('?' * len(kbs))})", tuple(kbs)


class QdrantStore:
    """Qdrant 컬렉션 하나 (거리 Cosine). qdrant-client 는 이 저장소를 고를 때만 불러온다.
    url 에 ':memory:' 를 주면 서버 없이 메모리에서 돈다 (시험용)."""

    def __init__(self, url=None, collection=None, api_key=None):
        try:
            from qdrant_client import QdrantClient, models
        except ImportError:
            raise StoreError("qdrant-client 가 설치되지 않았어요. (pip install qdrant-client)")
        self.m = models
        self.collection = collection or config.QDRANT_COLLECTION
        url = url or config.QDRANT_URL
        try:
            self.client = (QdrantClient(location=":memory:") if url == ":memory:"
                           else QdrantClient(url=url, api_key=(api_key or config.QDRANT_API_KEY) or None, timeout=30))
            self._ready = self.client.collection_exists(self.collection)
        except Exception:  # 연결 오류 종류가 버전마다 달라 한꺼번에 받는다 (주소는 문구에 넣지 않는다)
            raise StoreError("문서 저장소(Qdrant)에 연결할 수 없어요.")

    def _call(self, fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except StoreError:
            raise
        except Exception:
            raise StoreError("문서 저장소(Qdrant)에 연결할 수 없어요.")

    def _ensure(self, size):
        if not self._ready:
            self._call(self.client.create_collection, self.collection,
                       vectors_config=self.m.VectorParams(size=size, distance=self.m.Distance.COSINE))
            self._ready = True

    def _filter(self, kb=None, file_id=None, kbs=None):
        must = []
        if kb is not None:
            must.append(self.m.FieldCondition(key="kb", match=self.m.MatchValue(value=kb)))
        if file_id is not None:
            must.append(self.m.FieldCondition(key="file_id", match=self.m.MatchValue(value=file_id)))
        if kbs is not None:
            must.append(self.m.FieldCondition(key="kb", match=self.m.MatchAny(any=list(kbs))))
        return self.m.Filter(must=must)

    def replace_file(self, kb, file_id, file, hash_, model, texts, vectors):
        if not texts:
            self.delete_file(kb, file_id)
            return
        self._ensure(len(vectors[0]))
        self.delete_file(kb, file_id)
        points = [self.m.PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{kb}/{file_id}/{i}")), vector=[float(x) for x in v],
            payload={"kb": kb, "file_id": file_id, "file": file, "hash": hash_, "model": model, "idx": i, "text": t})
            for i, (t, v) in enumerate(zip(texts, vectors))]
        self._call(self.client.upsert, self.collection, points=points)

    def delete_file(self, kb, file_id):
        if self._ready:
            self._call(self.client.delete, self.collection,
                       points_selector=self.m.FilterSelector(filter=self._filter(kb=kb, file_id=file_id)))

    def _scroll(self, flt):
        """조건에 맞는 조각의 payload 전부 (벡터는 안 가져온다)."""
        if not self._ready:
            return []
        out, offset = [], None
        while True:
            points, offset = self._call(self.client.scroll, self.collection, scroll_filter=flt, limit=256,
                                        offset=offset, with_payload=True, with_vectors=False)
            out += [p.payload for p in points]
            if offset is None:
                return out

    def files(self, kb):
        return {p["file_id"]: p["hash"] for p in self._scroll(self._filter(kb=kb))}

    def file_names(self, kbs=None):
        flt = self._filter(kbs=kbs) if kbs is not None else None
        return _by_kb((p["kb"], p["file"]) for p in self._scroll(flt))

    def count(self, kbs=None):
        if not self._ready:
            return 0
        flt = self._filter(kbs=kbs) if kbs is not None else None
        return self._call(self.client.count, self.collection, count_filter=flt, exact=True).count

    def other_model(self, model):
        if not self._ready:
            return False
        flt = self.m.Filter(must_not=[self.m.FieldCondition(key="model", match=self.m.MatchValue(value=model))])
        return self._call(self.client.count, self.collection, count_filter=flt, exact=True).count > 0

    def search(self, vector, kbs=None, k=5):
        if not self._ready:
            return []
        flt = self._filter(kbs=kbs) if kbs is not None else None
        res = self._call(self.client.query_points, self.collection, query=[float(x) for x in vector],
                         query_filter=flt, limit=k, with_payload=True)
        return [{"kb": p.payload["kb"], "file": p.payload["file"], "score": float(p.score), "text": p.payload["text"]}
                for p in res.points]


_store = None


def open_store():
    """.env 의 VECTOR_STORE 에 맞는 저장소 (한 번 만들어 재사용)."""
    global _store
    if _store is None:
        if config.VECTOR_STORE == "qdrant":
            _store = QdrantStore()
        elif config.VECTOR_STORE == "sqlite":
            _store = SqliteStore()
        else:
            raise StoreError(f"VECTOR_STORE 값은 sqlite 또는 qdrant 여야 해요. (지금: {config.VECTOR_STORE})")
    return _store
