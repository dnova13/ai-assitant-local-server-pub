"""디스코드 채널 관리 분류기 시험 (실제 Ollama 필요). 문장을 하나씩 보내고, 오류가 나면 멈춘다.
  python -m scripts.channel_eval
관리 채널을 지정하기 전에 돌려 정확도를 본다. 단서가 없어 모델을 부르지 않는 문장은 '단서 없음'으로 맞다고 친다."""
import threading

from core import classifier
from features import channel_manage as C

SENTENCES = [  # (문장, 기대 동작)
    ("지난주에 민수가 올린 견적서 찾아줘", "찾기"), ("어제 사진 보여줘", "찾기"), ("이번 달 pdf 어디 있어?", "찾기"),
    ("서버 점검 얘기한 거 검색해줘", "찾기"), ("그 동영상 찾아줄래", "찾기"), ("민수가 올린 파일 목록 보여줘", "찾기"),
    ("10월 3일에 올린 첨부파일 찾아줘", "찾기"), ("회의록 어디 있는지 찾아줘", "찾기"),
    ("오늘 오후 1시 이전 올린 사진", "찾기"), ("최근 올린 사진", "찾기"), ("파일 뭐 올렸어?", "찾기"),
    ("이번 달 파일 다 보여줘", "찾기"), ("저번 달 올린 사진", "찾기"), ("오늘 올린 사진 다 보여줘", "찾기"),
    ("민수 광고 글 지워줘", "삭제"), ("어제 올린 사진 삭제해줘", "삭제"), ("이 채널 광고 다 지워줘", "삭제"),
    ("3일 전 메시지 없애줘", "삭제"), ("철수가 올린 견적서 삭제", "삭제"),
    ("ㅋㅋ 오늘 뭐 먹지", "아님"), ("내일 날씨 어때?", "아님"), ("안녕하세요", "아님"), ("우유 사기 메모해줘", "아님"),
    ("그 파일 어디 갔는지 모르겠네 ㅋㅋ", "아님"), ("사진 진짜 잘 나왔다", "아님"), ("나 어제 지갑 찾았어", "아님"),
    ("그거 지워야겠다 생각했어", "아님"), ("영상 재밌더라", "아님"), ("일정 잡아줘", "아님"),
]


def main():
    lock, ok = threading.Lock(), 0
    for text, want in SENTENCES:
        if not C.CLUE.search(text):
            got = "아님"
            note = "단서 없음"
        else:
            try:
                result = C.classify(text, lock)
            except classifier.ClassifierError as e:
                print(f"오류 {e}\n오류가 나서 멈춥니다. 서버 상태를 확인하세요 (ollama ps, journalctl -u ollama).")
                return
            got, note = (result["동작"], f"찾는말={result['찾는말']} 사람={result['사람']}") if result else ("형식 오류", "")
        good = got == want
        ok += good
        print(("OK  " if good else "X   ") + f"{text} -> {got} {note}" + ("" if good else f" (기대 {want})"))
    print(f"\n정확 {ok}/{len(SENTENCES)}")


if __name__ == "__main__":
    main()
