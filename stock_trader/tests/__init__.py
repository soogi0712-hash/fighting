"""테스트 패키지 루트.

tests 를 패키지로 만들어 하위 tests.journal / tests.phoenix 가 제품 패키지
(journal / phoenix)를 가리지 않도록 한다. 이 파일이 없으면 pytest 가 tests/ 를
sys.path 에 삽입해 `import journal` 이 tests/journal 로 잘못 해석된다(수집 충돌).
"""
