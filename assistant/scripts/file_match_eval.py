"""실제 Ollama의 기존 이름 비교 시험. assistant/에서 python -m scripts.file_match_eval.
후보는 전부 가짜 이름이다. 사용자 파일·DB는 읽지 않고 요청은 하나씩 보낸다. 연결·응답 오류면 멈춘다.
"""
from core import classifier
from features import files

CASES = [
    ("테스트", ["test", "테스트", "sample"], {"c1", "c2"}),
    ("test", ["test", "테스트", "sample"], {"c1", "c2"}),
    ("샘플", ["sample", "샘플", "simple"], {"c1", "c2"}),
    ("리포트", ["report.txt", "리포트.txt", "notes.txt"], {"c1", "c2"}),
    ("백업", ["backup", "백업", "archive"], {"c1", "c2"}),
    ("테스트", ["시험", "검사", "sample"], set()),
    ("메모.txt", ["메모.txt", "memo.txt", "memo.pdf"], {"c1", "c2"}),
    ("리포트", ["report_2026.txt", "리포트_2026.txt", "invoice.txt"], {"c1", "c2"}),
]


def main():
    correct = 0
    for number, (query, names, want) in enumerate(CASES, 1):
        candidates = [{"id": f"c{i}", "name": name} for i, name in enumerate(names, 1)]
        try:
            got = set(files.match_names(query, candidates))
        except (files.NameMatchError, classifier.ClassifierError) as e:
            print(f"#{number} 오류로 중단: {e}")
            return 1
        good = got == want
        correct += good
        print(f"{'O' if good else 'X'} #{number} {query}: 기대 {sorted(want)}, 결과 {sorted(got)}", flush=True)
    print(f"기존 이름 비교 {correct}/{len(CASES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
