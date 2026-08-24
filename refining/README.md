# refining — 정제·정유 트래커

`research-dashboard` 레포의 하위 프로젝트. 단일 스크롤 페이지.

공개 주소: `https://yuixy82-create.github.io/research-dashboard/refining/`

## 구성

| 장 | 내용 | 갱신 주기 |
|---|---|---|
| 1장 | 팔로우업 — 코어 6, 지표 추이 차트, 확장 8, 전체 목록, 시나리오, 반증 지표, 캘린더 | 주간 |
| 2장 | 리포트 요약 + 원문 PDF | 고정 |
| 3장 | 기업 분석 — 4사 비교, S-OIL, SK이노베이션, 증권사 추정치 | 분기 |
| 4장 | 자료·방법론 | 고정 |
| 5장 | 정유 산업 기초 | 고정 |

## 데이터

```
data/manual.json          수동 입력 지표 (값 + asOf)
data/auto.json            Actions가 덮어씀. 손대지 말 것
data/series/<key>.json    지표별 시계열. 차트 소스
```

브라우저에서 세 곳을 합쳐 렌더링하며 `auto.json`이 `manual.json`의 같은 key를 덮어씀.

### 시계열 형식

```json
{
  "key": "diesel_crack_1_1",
  "label": "디젤 1:1 크랙",
  "unit": "$/bbl",
  "demo": false,
  "points": [ {"d": "2026-08-17", "v": 102.2} ]
}
```

`demo: true`이면 카드에 "예시" 배지가 붙음. 실제 수집이 붙으면 `false`로 바꿀 것.
현재 `diesel_crack_1_1.json`만 예시 계열이며 나머지 5개는 빈 상태.

## 데이터 소스 계획

| 지표 | 소스 | 상태 |
|---|---|---|
| 디젤 1:1 크랙 · 3-2-1 크랙 | EIA API v2 (ULSD · RBOB · WTI 스팟) | 시리즈 ID 확정 필요 |
| 미국 중간유분 재고 | EIA API v2 (`WDISTUS1`) | 시리즈 ID 확정 필요 |
| 싱가포르 복합정제마진 | 오피넷 유가정보 API + 두바이유 → 가중 근사 | 항목 확인 필요 |
| Group III 기유 스프레드 | 관세청 품목별 수출입실적 API (HS 2710.19) − 싱가포르 HSFO | 스크립트 미작성 |
| PX − 나프타 스프레드 | 관세청 (HS 2902.43 · 2710.12) | 스크립트 미작성 |
| 러시아 디젤 해상수출 | Reuters 인용 | 수동 |

## 자동 수집 활성화 절차

1. EIA API 키 발급 → 레포 Secrets에 `EIA_API_KEY` 등록
2. 공공데이터포털 키 발급 → Secrets에 `DATA_GO_KR_KEY` 등록
3. `scripts/fetch_eia.py`의 시리즈 ID 확정 후 단독 실행 검증
4. 계산한 디젤 크랙을 Reuters 공시값과 대조
5. `.github/workflows/refining-update.yml`의 `schedule` 주석 해제

산출식:

```
디젤 1:1 크랙 = ULSD($/gal) × 42 − WTI($/bbl)
3-2-1 크랙    = (RBOB × 42 × 2 + ULSD × 42 × 1 − WTI × 3) ÷ 3
기유 스프레드  = 윤활유 수출단가($/t) − 싱가포르 HSFO($/t)
```

## 유의

개인 학습 및 기록 목적. 특정 종목의 매수·매도 권유가 아님.
