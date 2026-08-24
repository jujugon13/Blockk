# HTTP·WebSocket 어댑터

FastAPI·uvicorn에서 기존 REST·WebSocket 계약을 그대로 노출한다.

## 담당 명세

| 명세 절 | 내용 |
|---|---|
| `specs/01-purpose-and-boundary.md` FR-SYS-003·006~007 | 실행 역할·입력 채널·실행 인자 |
| `specs/09-recovery-and-side-effects.md` §15 | 프로세스 수명주기·재연결 |
| `specs/10-nfr-and-security.md` §18 | 인증·인가·CORS·비밀정보 |

## 담당 acceptance criteria

직접 소유 없음. F17의 `acceptance_ids`는 빈 배열이며 기존 AC를 통합시험으로 재검증한다.

## 의존

| 대상 | 경유 | 비고 |
|---|---|---|
| FastAPI·uvicorn | ASGI HTTP·WebSocket·수명주기 | REST·WebSocket·lifespan 완료, uvicorn 예정 |
| 기능 계약 | `src.shared` 포트와 `src/application.py` 조립 | 기능 폴더 동작 코드 수정 금지 |

## 이 폴더가 책임지지 않는 것

- 기능별 REST 처리 규칙과 상태 전이
- 검색·Worker·Sync 내부 구현
- uvicorn 전체 회귀
