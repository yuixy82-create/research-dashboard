# refining — 정제·정유 트래커

`research-dashboard` 레포의 하위 프로젝트. 단일 스크롤 페이지.

공개 주소: `https://yuixy82-create.github.io/research-dashboard/refining/`

## 구성

| 장 | 내용 | 갱신 주기 |
|---|---|---|
| 1장 | 팔로우업 — 코어 지표 9종(추이 차트) + 주간 보조 6종(표), 추후 일정, 진행 경과, 지표 후보 | 주간 |
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
자동 4종(`diesel_crack_1_1` · `crack_3_2_1` · `us_distillate_stock` · `px_naphtha_spread`)은 스크립트가
매 실행마다 파일 전체를 다시 쓰므로 손대지 않음. 나머지는 `scripts/add_point.py`로 점을 추가함(아래 주간 루틴).

## 데이터 소스

| 지표 | 소스 | 상태 |
|---|---|---|
| 디젤 1:1 크랙 | NYMEX 선물 `HO=F` · `CL=F` 근월물 (야후 파이낸스) | 자동 · 매 영업일, 하루 2회 |
| 3-2-1 크랙 | 위 2종 + `RB=F` | 자동 · 매 영업일, 하루 2회 |
| 미국 중간유분 재고 | EIA `WDISTUS1` | 자동 · 주간(수) |
| PX − 나프타 스프레드 | 관세청 품목별 수출입실적 (PX `2902430000` 수출단가 − 나프타 `2710124000` 수입단가) | 자동 · 월간, 약 2개월 지연 |
| 싱가포르 복합정제마진 · 경유·등유·휘발유 마진 · Ural−Dubai · 미국 가동률 | Petronet · EIA — iM증권 기름뿜뿜 위클리(금)에서 옮겨 적음 | 수동 · 주간 |
| 사우디 OSP · 러시아 디젤 해상수출 · 중국 정제품 수출 · 러시아 정제설비 피격 | Aramco · Reuters · 해관총서 — 같은 위클리 인용 | 수동 · 월간 |
| Group III 기유 스프레드 | 관세청 윤활유 기유 `2710195020` 수출단가 기반 산출 예정 | 미연결 |

산출식:

```
디젤 1:1 크랙 = ULSD($/gal) × 42 − WTI($/bbl)
3-2-1 크랙    = (RBOB × 42 × 2 + ULSD × 42 × 1 − WTI × 3) ÷ 3
```

22:00 KST 실행은 장중이라 마지막 점이 **잠정치**(`provisional: true`, 화면에 「잠정」 배지),
07:30 KST 실행에서 전일 정산가로 덮임. 매 실행마다 원본에서 구간 전체를 다시 만듦.
EIA 일간 스팟은 주 1회(수) 배치 공표라 쓰지 않음.

## 자동 수집

`.github/workflows/refining-update.yml` — 두 잡으로 나뉨.

| 잡 | 어디서 | 하는 일 |
|---|---|---|
| `fetch` | self-hosted 러너 (메인컴 DESKTOP-THUP7M4) | 야후·EIA·관세청 수집 → GitHub API로 `refining/data` 커밋 |
| `deploy` | ubuntu-latest | Pages 배포 (`upload-pages-artifact`가 bash·tar를 요구해 분리) |

| 항목 | 값 |
|---|---|
| 주기 | 매일 07:30 · 22:00 KST (UTC 22:30 · 13:00) |
| 수동 실행 | Actions 탭 → refining-update → Run workflow |
| 커밋 | 값이 바뀐 파일만 (updatedAt만 바뀐 건 무시). `scripts/ci_commit.py` |
| 실패 표시 | `auto.json`의 `errors`에 소스별 메시지. `status`는 `ok` / `partial` |

메인컴 러너를 쓰는 이유: 야후가 깃허브 러너 IP에 429를 줌. 러너 서비스 계정(NETWORK SERVICE)은
사용자 PATH를 못 보므로 워크플로 `env.PY`에 파이썬 절대경로를 넣고, 그 폴더에 `icacls`로 읽기 권한을 줌.
git도 없어 커밋은 Git Data API로 만듦. 메인컴이 꺼져 있으면 그 회차는 건너뜀(다음 회차에 따라잡음).

Secrets: `EIA_API_KEY` (api.eia.gov/register), `DATA_GO_KR_KEY` (공공데이터포털 → 관세청 품목별 수출입실적 활용신청).

## 주간 수동 루틴 (금요일 iM증권 위클리)

1. 리포트 PDF를 채팅에 올린다 (또는 드라이브 폴더).
2. 아래 값을 읽어 `scripts/add_point.py`로 점을 추가한다. `data/series/<key>.json`과 `manual.json`의
   value · asOf · note가 같이 바뀜.

```
python3 refining/scripts/add_point.py sing_grm           2026-09-04 30.1  --note "전주 대비 −2.7"
python3 refining/scripts/add_point.py gasoil_margin_sg   2026-09-04 60.0  --note "전주 대비 −2.4"
python3 refining/scripts/add_point.py kero_margin_sg     2026-09-04 52.0
python3 refining/scripts/add_point.py gasoline_margin_sg 2026-09-04 22.0
python3 refining/scripts/add_point.py ural_dubai         2026-09-04 -12.0
python3 refining/scripts/add_point.py us_refinery_util   2026-08-28 97.0  --base 2026-09-04
```

월간 지표(사우디 OSP 매월 5일 전후 · 러시아 해상수출 · 중국 수출 · 러시아 피격)는 나오는 주에만 추가.
3. `data/series/*.json` · `data/manual.json`을 업로드하면 `static.yml`이 Pages를 다시 배포함.

## 유의

개인 학습 및 기록 목적. 특정 종목의 매수·매도 권유가 아님.
