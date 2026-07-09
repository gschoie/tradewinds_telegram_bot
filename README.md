# TradeWinds Telegram Bot

TradeWinds(tradewindsnews.com) 검색 화면을 Chromium으로 열어 **쿠키/광고동의(CMP) 팝업을 DOM에서 제거한 뒤** 캡쳐해 텔레그램으로 보낸다. 스크린샷 API를 쓰지 않으므로 할당량 제한이 없고, 팝업 잔상도 남지 않는다.

기존 GAS 버전을 대체한다. GAS는 브라우저가 없어 팝업을 제거하지 못하고 ScreenshotOne 무료 할당량(월 100장)에 막혔다.

## 동작

1. 검색(`q=korea`) 페이지를 열고 팝업/광고/iframe 제거
2. 기사 링크를 추출해 **signature**(정렬된 링크 집합) 계산
3. 직전 실행과 비교
   - **새 뉴스 있음** → 🚢 화면 캡쳐 + 📰 기사 목록(최대 10개), signature 저장
   - **변화 없음** → 캡쳐 없이 ℹ️ 최근 뉴스 3개 링크만 텍스트로
4. 야간(KST 01~05시)에는 자동 스킵

## 로컬 실행

```powershell
cd .\tradewinds_telegram_bot
pip install -r .\requirements.txt
python -m playwright install chromium
copy .env.example .env   # 값 채우기
python .\tradewinds_telegram_bot.py --once
```

## GitHub Actions 배포

1. 이 폴더를 GitHub 리포로 올린다(또는 산업봇 리포에 함께 둔다).
2. 리포 **Settings → Secrets and variables → Actions**에 추가:
   - `TELEGRAM_BOT_TOKEN` — GS_Heavy_스냅샷(@gschoiebot) 봇 토큰
   - `TELEGRAM_CHAT_ID` — 받을 chat_id (개인이면 숫자)
3. Actions 탭에서 **TradeWinds Bot**을 수동 실행하거나 매시 스케줄을 기다린다.

상태(signature)는 워크플로의 `actions/cache`(롤링 키)로 실행 간 유지된다 — 리포에 커밋을 남기지 않는다.

## 옵션(환경변수)

- `TW_SEARCH_QUERY=korea` — 검색어
- `TW_ARTICLE_LIMIT=10`
- `TW_VIEWPORT_WIDTH=1280` / `TW_VIEWPORT_HEIGHT=900`
- `TW_FULL_PAGE=false` — true면 전체 페이지 캡쳐
- `TW_HEADLESS=true`
- `TELEGRAM_NOTIFY_ERRORS=true` — 오류 시 텔레그램 알림
- `TW_IGNORE_QUIET=true` — 야간 스킵 무시(테스트용)
