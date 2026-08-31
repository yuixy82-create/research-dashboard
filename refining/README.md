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

`demo: true`이면 카드에 "예시" 배지가 붙음.
자동 3종(`diesel_crack_1_1` · `crack_3_2_1` · `us_distillate_stock`)은 스크립트가 매 실행마다
파일 전체를 다시 쓰므로 손대지 않음. 나머지 4종은 빈 상태이며 값이 생기면 손으로 채움.

## 데이터 소스

| 지표 | 소스 | 상태 |
|---|---|---|
| 디젤 1:1 크랙 | EIA `RWTC` · `EER_EPD2DXL0_PF4_RGC_DPG` | 자동 · 일간 |
| 3-2-1 크랙 | 위 2종 + EIA `EER_EPMRU_PF4_RGC_DPG` | 자동 · 일간 |
| 미국 중간유분 재고 | EIA `WDISTUS1` | 자동 · 주간(수) |
| 싱가포르 복합정제마진 | Platts · 증권사 주간자료 | 수동 |
| Group III 기유 스프레드 | Argus · ICIS | 수동 |
| PX − 나프타 스프레드 | ICIS · 증권사 | 수동 |
| 러시아 디젤 해상수출 | Reuters 인용 | 수동 |

자동 3종은 전부 EIA 무료 API 하나로 끝남: 키 1개(`EIA_API_KEY`) 외에 다른 인증이 없음.
나머지 4종은 유료 평가가격이거나 인용치이므로 `data/manual.json`에 손으로 넣음.

산출식:

```
디젤 1:1 크랙 = ULSD($/gal) × 42 − WTI($/bbl)
3-2-1 크랙    = (RBOB × 42 × 2 + ULSD × 42 × 1 − WTI × 3) ÷ 3
```

세 스팟의 최신 날짜가 어긋날 수 있으므로 **날짜 교집합에서만** 계산함.
매 실행마다 EIA 원본에서 구간 전체를 다시 만듦: 사후 수정된 값도 따라감.

## 자동 수집

`.github/workflows/refining-update.yml`

| 항목 | 값 |
|---|---|
| 주기 | 매일 07:30 · 22:00 KST (UTC 22:30 · 13:00) |
| 수동 실행 | Actions 탭 → refining-update → Run workflow |
| 커밋 | `refining/data`에 변경이 있을 때만 |
| 배포 | 커밋이 `static.yml`을 깨워 Pages 재배포 |
| 실패 표시 | `auto.json`의 `status`가 `ok`가 아니면 워크플로가 빨간 X로 끝남 |

키 등록: 저장소 Settings → Secrets and variables → Actions → New repository secret →
이름 `EIA_API_KEY`, 값은 api.eia.gov/register에서 받은 키.

## 유의

개인 학습 및 기록 목적. 특정 종목의 매수·매도 권유가 아님.
