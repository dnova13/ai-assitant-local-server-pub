#!/usr/bin/env bash
# 최초 1회(또는 저장소 맨 위 requirements.txt 변경 시) 실행: 가상환경 생성 + 패키지 설치 + .env·documents.json 준비 + 단위 시험
# 가상환경은 저장소 맨 위 .venv 를 쓴다. 여러 번 실행해도 안전하다 (이미 있는 .venv, .env, documents.json 은 그대로 둔다)
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ python3 가 없습니다. 설치: sudo apt install python3" >&2
  exit 1
fi

VENV=../.venv
if [ ! -d "$VENV" ]; then
  echo "▶ 가상환경 생성 (저장소 맨 위 .venv)"
  if ! python3 -m venv "$VENV"; then
    rm -rf "$VENV"
    echo "❌ venv 생성 실패. 우분투에서는 보통 아래 설치가 필요합니다:" >&2
    echo "   sudo apt install python3-venv python3-pip" >&2
    exit 1
  fi
else
  echo "▶ 기존 가상환경 사용 (저장소 맨 위 .venv)"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "▶ 패키지 설치"
python -m pip install --upgrade pip -q
python -m pip install -r ../requirements.txt -q

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "▶ .env 를 만들었습니다 (권한 600). 값을 채워 주세요: nano .env"
else
  echo "▶ .env 이미 있음 (그대로 유지)"
fi

if [ ! -f documents.json ]; then
  cp documents.example.json documents.json
  echo "▶ documents.json 을 만들었습니다. 문서 검색을 쓰려면 지식 기반 ID 를 채워 주세요"
else
  echo "▶ documents.json 이미 있음 (그대로 유지)"
fi

echo "▶ 단위 시험 (실제 서버를 부르지 않음)"
python -m unittest discover -s tests 2>&1 | grep -E '^(Ran |OK|FAILED)' || true

cat <<'EOF'

✅ 설치 완료. 다음 할 일 (자세한 내용: ../README.md)
  1) nano .env                                        Ollama·디스코드·Open WebUI 값
  2) ollama pull bge-m3                               문서 검색 임베딩 모델
  3) ../.venv/bin/python -m scripts.doc_index            문서 색인 (documents.json 채운 뒤)
  4) ../.venv/bin/python -m core.apikeys create owner "디스코드 봇" --kind bot   → .env 의 BOT_API_KEY
  5) deploy/*.service 를 /etc/systemd/system/ 에 등록  (../README.md "배포와 컴퓨터 제어")
  직접 실행: ./run.sh [api|bot|cli|test]
EOF
