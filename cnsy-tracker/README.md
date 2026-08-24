# cnsy-tracker

Cerenome, Inc.(NASDAQ: CNSY / 구 Plus Therapeutics, PSTV) 추적기.

공개 주소: `../cnsy-tracker/`

## 구성

```
cnsy-tracker/
├── index.html            문서 본문. data/cache.json을 읽어 지표·로그를 채움
├── scripts/fetch_all.py  수집 (표준 라이브러리만)
└── data/
    ├── seed.json         수동 기초값. cache.json이 없을 때의 폴백
    └── cache.json        자동 누적 (Actions가 커밋)
```

워크플로: `.github/workflows/cnsy-update.yml` · 하루 2회 + 수동 실행.

## 즉시 갱신

저장소 상단 **Actions** → 좌측 **cnsy-tracker update** → 우측 **Run workflow**.

## 추적 대상

| 소스 | 잡는 것 |
|---|---|
| SEC submissions API (CIK 0001095981) | 제출물 신규 |
| SEC XBRL `dei:EntityCommonStockSharesOutstanding` | 발행주식수 → ATM 희석 |
| ir.cerenome.com RSS | 보도자료 · 공시 |
| respect-trials.com WP API | 투여 환자수 카운터 |
| cnside-dx.com WP API | 제품 · 랩 소식 |
| ClinicalTrials.gov API v2 | 등록자수 · 상태 |
| CMS CLFS 연례공청회 페이지 | CY2027 가격결정 파일 등장 |
| Yahoo Finance chart | 주가 |
| 레딧 | 미확인 신호 |

## 주의

- 회사는 사명·티커를 바꿨으나 **CIK 0001095981은 불변**. SEC 메타데이터에는 구 사명이 남아 있으므로 티커로 조회하지 않음.
- ClinicalTrials.gov 스폰서명도 구 사명으로 남아 있어 `Cerenome`과 `Plus Therapeutics` 양쪽으로 조회함.
- 소스 하나가 실패해도 나머지는 갱신됨. 실패분은 직전 값을 유지하고 `meta.status`에 기록됨.

본 자료는 개인 기록 목적이며 특정 종목의 매수 · 매도 권유가 아님.
