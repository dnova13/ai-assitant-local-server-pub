"""파일 분류 시험. 파일 문장을 실제 모델에 보내 동작과 인자(client_action)가 맞는지 본다.
실행 (assistant/ 폴더, 서버 Ollama에 닿아야 함):
  python -m scripts.file_eval                         # .env 의 ASSISTANT_MODEL
  python -m scripts.file_eval --model llama3.1:8b
  python -m scripts.file_eval --model qwen3:8b --think off   # 생각을 끄고 시험
기대값: 동작('file.…', '되묻기', '거절', '아님')과 args (되묻기·거절·아님이면 None).
'아님'은 거르기(looks_like_file)를 통과했지만 파일 요청이 아니어서 평소 분류로 넘어가야 하는 문장이다."""
import argparse

from core import classifier
from features import files

ASK, REFUSE, NOT, SKIP = "되묻기", "거절", "아님", "거르기에서 빠짐"
CASES = [
    (1, "다운로드에 있는 견적서.pdf 바탕화면으로 옮겨줘", "file.move", {"target": "견적서.pdf", "dest": "desktop", "root": "downloads"}),
    (2, "보고서.hwp 문서 폴더에 복사해줘", "file.copy", {"target": "보고서.hwp", "dest": "documents"}),
    (3, "작년 세금 파일 찾아줘", "file.search", {"query": "작년 세금"}),
    (4, "여행사진.png 열어줘", "file.open", {"target": "여행사진.png"}),
    (5, "이력서.docx 어디 있어? 폴더 열어줘", "file.reveal", {"target": "이력서.docx"}),
    (6, "메모.txt 이름 할일.txt로 바꿔줘", "file.rename", {"target": "메모.txt", "name": "할일.txt"}),
    (7, "바탕화면에 프로젝트 폴더 만들어줘", "file.mkdir", {"dest": "desktop", "name": "프로젝트"}),
    (8, "정산.xlsx 백업해줘", "file.backup", {"target": "정산.xlsx"}),
    (9, "다운로드의 old.zip 삭제해줘", "file.delete", {"target": "old.zip", "root": "downloads"}),
    (10, "결과.pdf 보내줘", "file.send", {"target": "결과.pdf"}),
    (11, "회의록.docx를 다운로드 폴더로 이동시켜", "file.move", {"target": "회의록.docx", "dest": "downloads"}),
    (12, "계약서 파일 어디 있는지 보여줘", "file.reveal", {"target": "계약서"}),
    (13, "문서 폴더에서 영수증 찾아 줘", "file.search", {"query": "영수증", "root": "documents"}),
    (14, "강의노트.pdf 바탕화면에도 복사해 둬", "file.copy", {"target": "강의노트.pdf", "dest": "desktop"}),
    (15, "사진 폴더 안에 2026 폴더 만들어 줘", "file.mkdir", {"dest": "사진", "name": "2026"}),
    (16, "이 파일 옮겨줘", ASK, None),
    (17, "보고서.pdf 옮겨줘", ASK, None),
    (18, "config.json 내용 수정해줘", REFUSE, None),
    (19, "파일 이름 짓는 요령 알려줘", NOT, None),
    (20, "다운로드 속도가 느려", NOT, None),
    (21, "폴더 구조 어떻게 짜는 게 좋아?", NOT, None),
    (22, "백업 프로그램 추천해줘", NOT, None),
    (23, "엑셀 파일 합치는 방법", NOT, None),
    (24, "문서에 메모.txt 만들어 줘", "file.create", {"dest": "documents", "name": "메모.txt"}),
    (25, "바탕화면에 할일 파일 만들어 줘", "file.create", {"dest": "desktop", "name": "할일"}),
    (26, "다운로드에 정리 폴더 만들어 줘", "file.mkdir", {"dest": "downloads", "name": "정리"}),
    (27, "파일 test 에서 메모 추가해", "file.create", {"dest": "test", "name": "메모"}),
    (28, "문서에 메모.txt 등록해줘", "file.create", {"dest": "documents", "name": "메모.txt"}),
    (29, "바탕화면에 할일 파일 생성해", "file.create", {"dest": "desktop", "name": "할일"}),
    (30, "test 폴더에 자료 폴더 추가해", "file.mkdir", {"dest": "test", "name": "자료"}),
    (31, "메모.txt에 내용 추가해", REFUSE, None),
    (32, "다운로드에 정리.txt 추가해줘", "file.create", {"dest": "downloads", "name": "정리.txt"}),
    (33, "파일 test에서 목록 보여줘", "file.list", {"dest": "test"}),
    (34, "파일 테스트에 있는 목록 보여줘", "file.list", {"dest": "테스트"}),
    (35, "my music 폴더 안의 파일들 보여줘", "file.list", {"dest": "my music"}),
    (36, "문서 폴더에 뭐가 있어?", "file.list", {"dest": "documents"}),
    (37, "문서 안 test 폴더 목록 보여줘", "file.list", {"dest": "test", "root": "documents"}),
    (38, "파일 목록 보여줘", ASK, None),
    (39, "이 폴더 목록 보여줘", ASK, None),
    (40, "파일 명령어 목록 알려줘", NOT, None),
    (41, "파일 my music에서 메모 파일 열어", "file.open", {"target": "메모", "root": "my music"}),
    (42, "파일 메모 파일 열어", "file.open", {"target": "메모"}),
    (43, "파일 test/my music에서 회의 자료.txt 열어줘", "file.open", {"target": "회의 자료.txt", "root": "test/my music"}),
    (44, "파일 이 폴더에서 메모 열어줘", ASK, None),
    (45, "파일 my music에서 메모 파일 위치 보여줘", "file.reveal", {"target": "메모", "root": "my music"}),
    (46, "파일 메모 파일 위치 보여줘", "file.reveal", {"target": "메모"}),
    (47, "파일 my music에서 메모 이름을 새메모.txt로 바꿔줘", "file.rename", {"target": "메모", "name": "새메모.txt", "root": "my music"}),
    (48, "파일 메모 이름을 새메모.txt로 바꿔줘", "file.rename", {"target": "메모", "name": "새메모.txt"}),
    (49, "파일 이 폴더에서 메모 이름을 새메모.txt로 바꿔줘", ASK, None),
    (50, "파일 이 폴더에서 메모 위치 보여줘", ASK, None),
    (51, "파일 코드2를 my music 폴더에 복사해줘", "file.copy", {"target": "코드2", "dest": "my music"}),
    (52, "파일 test 폴더에서 코드2를 my music 폴더로 복사해줘", "file.copy", {"target": "코드2", "dest": "my music", "root": "test"}),
    (53, "파일 test 폴더에서 코드2를 my music 폴더로 옮겨줘", "file.move", {"target": "코드2", "dest": "my music", "root": "test"}),
    (54, "파일 코드2를 같은 폴더에 하나 더 복사해줘", "file.copy", {"target": "코드2", "sameFolder": True}),
    (55, "파일 코드2를 같은 폴더에 새코드라는 이름으로 복사해줘", "file.copy", {"target": "코드2", "name": "새코드", "sameFolder": True}),
    (56, "파일 test에서 코드2를 같은 폴더에 새코드라는 이름으로 복사해줘", "file.copy", {"target": "코드2", "name": "새코드", "sameFolder": True, "root": "test"}),
    (57, "파일 코드2 복사해줘", "file.copy", {"target": "코드2", "sameFolder": True}),
    (58, "파일 메모.txt를 같은 폴더에 복사해줘", "file.copy", {"target": "메모.txt", "sameFolder": True}),
    (59, "파일 코드2 옮겨줘", ASK, None),
    (60, "파일 이 폴더에서 코드2를 my music 폴더로 옮겨줘", ASK, None),
    (61, "파일 test에서 메모 경로 보여줘", "file.reveal", {"target": "메모", "root": "test"}),
    (62, "파일 test에서 메모 위치 보여줘", "file.reveal", {"target": "메모", "root": "test"}),
    (63, "파일 test/my music에서 메모 열어줘", "file.open", {"target": "메모", "root": "test/my music"}),
    (64, "파일 my music에서 메모 복사", "file.copy", {"target": "메모", "sameFolder": True, "root": "my music"}),
    (65, "파일 my music에서 메모 생성", "file.create", {"dest": "my music", "name": "메모"}),
    (66, "파일 my music에서 메모 백업해줘", "file.backup", {"target": "메모", "root": "my music"}),
    (67, "파일 my music에서 메모 삭제해줘", "file.delete", {"target": "메모", "root": "my music"}),
    (68, "파일 my music에서 메모 보내줘", "file.send", {"target": "메모", "root": "my music"}),
    (69, "파일 test/my music에서 메모 찾아줘", "file.search", {"query": "메모", "root": "test/my music"}),

]


def outcome(text, model=None):
    """(동작, args) — 실제 비서와 같은 순서: 거르기 -> 파일 분류 -> client_action 만들기."""
    if not files.looks_like_file(text):
        return SKIP, None
    r = files.classify(text, model=model)
    if r is None:
        return "형식 오류", None
    if r["동작"] == "아님":
        return NOT, None
    action, _ = files.build(r)
    if action is None:
        return (REFUSE if r["동작"] == "수정" else ASK), None
    return action["action"], action["args"]


def main():
    ap = argparse.ArgumentParser(description="파일 분류 시험")
    ap.add_argument("--model", default=None)
    ap.add_argument("--think", choices=("on", "off"), help="생각 기능이 있는 모델의 생각 켜기·끄기 (주지 않으면 Ollama 기본값)")
    a = ap.parse_args()
    classifier.set_think(a.think)
    ok_action = ok_all = 0
    wrong = []
    for n, text, want_action, want_args in CASES:
        got_action, got_args = outcome(text, a.model)
        ok_action += got_action == want_action
        good = got_action == want_action and got_args == want_args
        ok_all += good
        if not good:
            wrong.append(f"  X #{n} {text}\n      기대 {want_action} {want_args} / 결과 {got_action} {got_args}")
    print(f"파일 문장 {len(CASES)}개: 동작 정확 {ok_action}/{len(CASES)}, 동작+인자 정확 {ok_all}/{len(CASES)}")
    print("\n".join(wrong) if wrong else "틀린 문장 없음")


if __name__ == "__main__":
    main()
