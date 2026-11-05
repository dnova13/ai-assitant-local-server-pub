# 로컬 LLM 기반 AI 비서 서버

집 서버의 GPU 한 장으로 대화·문서 검색과 개인 비서 기능을 운영하는 프로젝트입니다. 디스코드·터미널·데스크톱 앱이 공통 비서 로직을 사용합니다.

모델은 문장의 의도와 필요한 값을 추출하고, 코드는 입력 검증·조회·저장·실행을 담당합니다. 외부 AI 모델 API는 사용하지 않으며 날씨·지역 검색·뉴스는 외부 데이터 서비스를 이용합니다.

## 구현한 기능

| 기능 | 내용 |
| --- | --- |
| 날씨·메모 | 지역·날짜를 해석한 날씨 조회, 사용자별 메모 저장·검색·요약·삭제 |
| 일정·브리핑 | 일정 등록, 사전·정시 알림, 놓친 일정 안내, 날씨·일정·메모 브리핑 |
| 뉴스 | RSS 제목·출처·링크 조회, 채널별 구독·게시, 중복 제목 제외 |
| 문서 검색 | Open WebUI 문서 수집, bge-m3 임베딩, SQLite 또는 Qdrant 검색, 근거·출처 표시 |
| 서버·DB 조회 | CPU·메모리·디스크·GPU·로드된 모델 조회, 관리자용 읽기 전용 DB 조회 |
| 컴퓨터 제어 | 등록한 장치의 SSH 절전·재시작, Wake-on-LAN 깨우기 |
| 앱 파일 요청 | 찾기·열기·위치 보기·이동·복사·이름 변경·폴더 생성·백업·삭제·보내기·빈 파일 생성·목록 조회의 12종 의도 전달 |
| 원격 PC 연결 | PC 등록·이름 변경·WebSocket 연결, 디스코드 요청·확인·결과 중계와 파일 송수신 |
| 채널 관리 | 지정 채널의 메시지·첨부파일 검색, 관리자 확인 후 메시지 삭제 |
| 사용자 분리 | 사용자별 데이터, 디스코드·앱 ID 연결, 관리자·일반 사용자 권한 |

PC의 실제 파일 탐색·확인·실행과 캐릭터별 기록 저장은 별도 데스크톱 앱이 담당합니다. 이 저장소는 비서 서버와 디스코드 봇의 구현을 포함합니다.

## 구조와 기술

| 경로 | 역할 |
| --- | --- |
| `assistant/entrances/` | CLI·디스코드 봇·FastAPI 입구 |
| `assistant/core/` | 공통 비서 로직, 분류기, 설정, 사용자·API 키 관리 |
| `assistant/features/` | 날씨·메모·일정·뉴스·RAG·컴퓨터 제어 등 기능 |
| `assistant/clients/` | Ollama·Open WebUI·DB·벡터 저장소 연결 |
| `assistant/scripts/` | 문서 색인·DB 이전·실제 모델 평가 |
| `assistant/tests/` | 외부 서비스를 모의 처리하는 자동 시험 |
| `assistant/deploy/` | systemd 서비스와 PostgreSQL·Qdrant 배포 예시 |
| `tools/` | 커밋 메시지·코드 리뷰·모델 벤치마크 도구 |

- 서버: Python, FastAPI, discord.py, WebSocket
- 모델: Ollama, 기본 비서 모델 `qwen2.5-coder:7b`, 임베딩 `bge-m3`
- 저장소: SQLite 또는 PostgreSQL, SQLite·numpy 또는 Qdrant
- 문서 관리: Open WebUI
- 운영: Ubuntu, systemd, Docker
- 개발·측정 환경: NVIDIA RTX 2060 6GB

같은 한국어 요청의 의도 분류는 7B가 86/89, 8B가 85/89였습니다. 문맥 4096의 성능 시험에서 7B는 GPU에 전부, 8B는 86% 적재돼 제한된 GPU 메모리에서의 응답 성능을 고려해 7B를 선택했습니다. 해당 시험 문장·환경에 대한 기록입니다.

## 설치와 실행

Python 3와 Ollama가 필요합니다. 문서 업로드·관리는 Open WebUI를 사용합니다.

```bash
git clone https://github.com/dnova13/ai-assitant-local-server-pub.git
cd ai-assitant-local-server-pub/assistant
bash setup.sh
```

설치 스크립트는 저장소 루트에 `.venv`를 준비하고, 없을 때만 `.env`와 `documents.json`을 예시 파일에서 복사합니다. [환경 설정 예시](assistant/.env.example)를 참고해 `assistant/.env`를 편집합니다.

| 설정 | 용도 |
| --- | --- |
| `OLLAMA_URL`, `ASSISTANT_MODEL` | 모델 서버와 비서 모델 |
| `OLLAMA_API_KEY` | Ollama 앞에 인증을 둔 경우의 키 |
| `DISCORD_TOKEN`, `ALLOWED_USER_IDS` | 디스코드 봇과 초기 관리자 연결 |
| `BOT_API_KEY`, `API_URL` | 봇에서 비서 API로 접속 |
| `DB_BACKEND` | `sqlite` 또는 `postgres`, 기본 `sqlite` |
| `VECTOR_STORE` | `sqlite` 또는 `qdrant`, 기본 `sqlite` |
| `OPENWEBUI_URL`, `OPENWEBUI_API_KEY` | 문서 색인용 Open WebUI 연결 |
| `SYSADMIN_CONTROL` | 컴퓨터 전원 제어 허용, 기본 꺼짐 |

```bash
# assistant/ 폴더에서 가상환경 사용
source ../.venv/bin/activate
ollama pull qwen2.5-coder:7b
python -m core.users init
python -m core.apikeys create owner "디스코드 봇" --kind bot
```

발급한 봇 키를 `.env`의 `BOT_API_KEY`에 넣습니다. 앱 키는 `python -m core.apikeys create owner "내 앱"`으로 별도 발급합니다. 키는 발급할 때 한 번만 표시합니다.

```bash
bash run.sh api   # 비서 API, 기본 포트 8050
bash run.sh bot   # 별도 터미널에서 실행. API가 먼저 켜져 있어야 함
bash run.sh cli   # 터미널에서 비서 본체 직접 사용
```

디스코드 개발자 포털에서 봇을 만들고 Message Content Intent를 켭니다. 필요한 서버·채널 권한과 본인 사용자 ID를 설정합니다. 자연어 대화 허용 범위는 `DISCORD_CHAT_GUILDS`·`DISCORD_CHAT_CHANNELS`로 제한합니다.

## 사용법과 API

명령어는 `!` 또는 `/`로 시작합니다. 디스코드의 `!도움말`에서 현재 명령어 목록을 확인합니다.

| 예시 | 동작 |
| --- | --- |
| `!날씨 부산` | 날씨 조회 |
| `!메모 우유 사기` | 메모 저장 |
| `!일정 목록` | 일정 조회 |
| `!브리핑` | 날씨·일정·메모 조회 |
| `!뉴스` | 뉴스 조회 |
| `!문서 기술문서 도커 설정` | 색인한 문서 검색 |
| `!상태` | 서버 상태 조회 |
| `!컴퓨터` | 등록한 컴퓨터와 사용 가능한 작업 확인 |

| API 경로 | 용도 |
| --- | --- |
| `/api/…` | Ollama 호환 입구 |
| `/v1/…` | 앱용 대화·파일 요청·확인·기기 등록 |
| `/v1/computers/connect` | PC 앱이 먼저 여는 WebSocket 연결 |
| `/internal/…` | 봇 키와 서버 내부 접속을 확인하는 봇 전용 API |

서버 실행 후 `/docs`에서 FastAPI의 HTTP API 명세를 확인할 수 있습니다.

## 문서 검색

Open WebUI에 문서를 업로드해 지식 기반을 만듭니다. [문서 설정 예시](assistant/documents.example.json)를 복사한 `documents.json`에 지식 기반 ID를 입력합니다.

```bash
# assistant/ 폴더와 가상환경에서 실행
ollama pull bge-m3
python -m scripts.doc_index --list
python -m scripts.doc_index
```

문서를 조각으로 나누고 임베딩해 선택한 벡터 저장소에 색인합니다. 검색 점수가 기준 미달이면 답변 생성 모델을 호출하지 않습니다. 기준을 통과한 조각은 답변 근거와 출처로 사용합니다. 점수 기준만으로 모든 문서 밖 질문을 판별할 수 있는 것은 아닙니다.

Qdrant 사용 시 `VECTOR_STORE=qdrant`와 연결 주소·키를 설정합니다. 저장소나 임베딩 모델을 바꾼 경우 `python -m scripts.doc_index --rebuild`로 다시 색인합니다.

## 배포와 컴퓨터 제어

[API 서비스](assistant/deploy/assistant-api.service)·[봇 서비스](assistant/deploy/assistant-bot.service)의 사용자명·실행 경로를 바꿔 systemd로 운영할 수 있습니다.

PostgreSQL·Qdrant 선택 시 해당 [DB 배포 예시](assistant/deploy/postgres/docker-compose.yml)·[벡터 저장소 배포 예시](assistant/deploy/qdrant/docker-compose.yml)를 사용합니다. 아래 명령은 `assistant/`에서 실행합니다.

```bash
docker compose --env-file .env -f deploy/postgres/docker-compose.yml up -d
docker compose --env-file .env -f deploy/qdrant/docker-compose.yml up -d
```

SQLite에서 PostgreSQL로의 이전 도구는 `scripts/migrate_to_postgres.py`입니다. 원본 SQLite를 보존하고 비어 있는 대상 DB에 이전하도록 구성돼 있습니다.

컴퓨터 제어는 관리자 권한과 등록된 장치를 기준으로 동작합니다. 대상 PC에 제한된 SSH 키와 절전·재시작 권한을 설정해야 합니다. 절전은 한 번, 재시작은 두 번 확인합니다. WOL은 신호 전송과 실제 부팅 완료를 구분합니다. 비서 서버 자체가 절전하면 봇도 동작하지 않습니다.

PC 파일 작업은 별도 앱의 원격 연결과 허용 폴더·인증 설정이 필요합니다. 이 서버만 실행해서 PC 파일을 직접 변경하는 구조는 아닙니다.

## 인증과 실행 경계

- API 키는 DB에 SHA-256 해시로 저장하고 앱 키·봇 키를 구분합니다.
- 사용자별 메모·일정·PC 등록을 분리하고 관리자 작업은 권한을 확인합니다.
- 읽기 전용 DB 조회는 대상 테이블·컬럼·권한·실행 시간을 제한합니다.
- PC 파일 실행은 앱에서 경로·원격 허용 범위·인증·사용자 확인을 검사합니다.
- 실제 환경 파일·DB·벡터 저장소·운영 로그·개인 키는 공개 대상에 포함하지 않습니다.
- 외부 API 제공 시 본인 환경에 맞는 HTTPS·인증·접근 제한을 구성합니다.

## 검증과 모델 평가

```bash
# assistant/ 폴더에서 실행. 실제 서비스 대신 모의 응답을 사용하는 단위 시험
python -m unittest discover -s tests

# 실제 로컬 모델 평가. 모델 서버 연결 필요
python -m scripts.classifier_eval
python -m scripts.file_eval
python -m scripts.doc_eval --route
```

공개용 기능 정리 후 임시 SQLite·모의 외부 응답으로 896개 시험을 실행했습니다. 실패·오류는 없었고 실제 PostgreSQL 연결을 요구하는 5개는 건너뛰었습니다. 모든 운영 환경과 외부 서비스의 통합 검증을 의미하지 않습니다.

별도 모델 벤치마크 도구는 저장소 루트의 `tools/model_benchmark.py`·`tools/model_perf.py`입니다. 각각 `--help`로 옵션을 확인합니다. 실제 모델 평가는 요청을 순서대로 보내고 오류가 나면 중단해 GPU 대기열이 누적되지 않도록 합니다.
