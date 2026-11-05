"""문서 색인 (README.md). 서버에서 사람이 실행한다. Open WebUI 지식 기반 -> 조각 -> bge-m3 임베딩 -> 벡터 저장소.
  python -m scripts.doc_index --list      # 지식 기반 ID·이름·파일 수 (documents.json 에 ID를 적을 때)
  python -m scripts.doc_index             # documents.json 의 지식 기반을 색인 (글이 같은 파일은 건너뜀, 사라진 파일은 지움)
  python -m scripts.doc_index --rebuild   # 전부 다시 (저장소나 임베딩 모델을 바꿨을 때)
저장소는 .env 의 VECTOR_STORE(sqlite/qdrant). 임베딩 요청은 하나씩 보내고, 오류가 나면 바로 멈춘다."""
import argparse
import sys
import time

from core import config
from clients import openwebui_client, vector_store
from features import documents


def main():
    ap = argparse.ArgumentParser(description="문서 색인")
    ap.add_argument("--list", action="store_true", help="지식 기반 목록만 보기")
    ap.add_argument("--rebuild", action="store_true", help="글이 같아도 전부 다시 색인")
    a = ap.parse_args()
    try:
        if a.list:
            for k in openwebui_client.knowledge_list():
                print(f"{k['id']}  {k['name']}  (파일 {k['files']}개)")
            return 0
        cfg = documents.load_config()
        if not documents.all_knowledge(cfg):
            print("documents.json 에 지식 기반 ID가 없어요. --list 로 ID를 보고 카테고리의 knowledge 에 적어 주세요.")
            return 1
        print(f"저장소 {config.VECTOR_STORE}, 임베딩 {config.EMBED_MODEL}")
        t = time.time()
        stats = documents.index(cfg, vector_store.open_store(), rebuild=a.rebuild)
    except (openwebui_client.OpenWebUIError, vector_store.StoreError, documents.DocumentError) as e:
        print(f"오류: {e}\n멈춥니다. 서버 상태를 확인하세요 (ollama ps, journalctl -u ollama).")
        return 1
    print(f"\n완료 {time.time() - t:.0f}초: 색인 {stats['added']}개 파일(조각 {stats['chunks']}개), "
          f"그대로 {stats['skipped']}개, 지움 {stats['deleted']}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
