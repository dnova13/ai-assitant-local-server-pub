# Continue.dev 시험용 샘플 (README.md 참고)
# 일부러 허술하게 만들었다. 이 파일은 시험용으로 복사해서 쓰고, 저장소 밖(예: 바탕화면)에서 연다.
#
# 시험 항목
#  - 채팅  : read_numbers 전체를 선택하고 "이 함수가 뭐 하는지 한국어로 설명해줘"
#  - 편집  : average 선택 후 "빈 리스트일 때 0을 돌려주도록 고쳐줘"
#  - 편집2 : read_numbers 선택 후 "with문으로 파일을 닫고 숫자가 아닌 줄은 건너뛰게 고쳐줘"
#  - 리뷰  : "이 코드에서 문제점 찾아줘" (top_n의 sort()가 원본을 바꾸는 점 등)
#  - 자동완성: 맨 아래 median 함수 안에서 타이핑


def read_numbers(path):
    f = open(path)
    nums = []
    for line in f:
        nums.append(int(line))
    return nums


def average(nums):
    return sum(nums) / len(nums)


def top_n(nums, n):
    # 큰 수부터 n개를 돌려준다
    nums.sort()
    return nums[-n:]


def summarize(path):
    nums = read_numbers(path)
    print("평균:", average(nums))
    print("상위 3개:", top_n(nums, 3))


def median(nums):
    # 중앙값을 돌려준다
    nums.sort()
    mid = len(nums) // 2
    if len(nums) % 2 == 0:
        return (nums[mid - 1] + nums[mid]) / 2
    else:
        return nums[mid]
