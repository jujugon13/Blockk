# PostgreSQL 검색 어댑터

확인된 벡터 검색 경로만 pgvector로 shared 검색 포트에 맞춰 제공한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/14-search-and-rag.md` S3.1·S4·S12·S14 | 모드별 검색·점수·오류·설정 |
| `specs/10-nfr-and-security.md` §17 | 트랜잭션·동시성·성능 경계 |

## 담당 acceptance criteria

직접 소유 없음. F17의 `acceptance_ids`는 빈 배열이며 기존 AC를 통합시험으로 재검증한다.

## 의존

| 대상 | 경유 | 비고 |
|---|---|---|
| pgvector 0.8.1 | 11단계 PostgreSQL 연결·UoW | 별도 연결 경계 생성 금지 |
| 검색 계약 | `src.shared.VectorSearcher` | 검색 기능 폴더 직접 참조 금지 |

## 이 폴더가 책임지지 않는 것

- 파이프라인 단계 순서·조건·추적 이름
- PostgreSQL 기본 순위를 BM25로 재명명하는 동작
- 확인되지 않은 KEYWORD·HYBRID 검색 경로
- 14단계 전 구현
