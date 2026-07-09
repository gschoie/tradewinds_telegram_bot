"""TradeWinds(tradewindsnews.com) 검색 화면 캡쳐 → 텔레그램.

산업/매크로 섹션 봇과 같은 배관(Playwright + 텔레그램)을 재사용한다.
GAS 버전과 달리 Playwright로 페이지를 직접 열어 쿠키/광고동의(CMP) 팝업을
DOM에서 제거한 뒤 캡쳐하므로 잔상이 남지 않는다. 스크린샷 API도 필요 없다.

동작:
  1) TradeWinds 검색(q=korea) 페이지를 열고 팝업/광고/iframe 제거
  2) 기사 링크를 추출해 signature(정렬된 링크 집합) 계산
  3) 직전 실행 signature와 비교
       - 새 뉴스 있음 → 화면 캡쳐 + 기사 목록(최대 10) 전송, signature 저장
       - 변화 없음   → 캡쳐 없이 최근 뉴스 3개 링크만 텍스트로 전송
  4) 야간(KST 01~05시)에는 실행하지 않는다.

필수 환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
상태 저장: tradewinds_state.json (GitHub Actions 캐시로 실행 간 유지)
"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

KST = timezone(timedelta(hours=9), name="KST")
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "tradewinds_captures"
STATE_PATH = BASE_DIR / "tradewinds_state.json"

SEARCH_QUERY = os.getenv("TW_SEARCH_QUERY", "korea")
SEARCH_URL = f"https://www.tradewindsnews.com/archive/?q={urllib.parse.quote(SEARCH_QUERY)}"
ARTICLE_LIMIT = int(os.getenv("TW_ARTICLE_LIMIT", "10"))


# ────────────────────────── 환경/텔레그램 ──────────────────────────
def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def telegram_api(method: str) -> str:
    return f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/{method}"


def post_form(method: str, fields: dict[str, str]) -> None:
    payload = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        telegram_api(method), data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        response.read()


def post_multipart(method: str, fields: dict[str, str], files: dict[str, Path]) -> None:
    boundary = f"----tw-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks += [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode("utf-8"), b"\r\n",
        ]
    for name, path in files.items():
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks += [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'.encode(),
            f"Content-Type: {mime_type}\r\n\r\n".encode(),
            path.read_bytes(), b"\r\n",
        ]
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    request = urllib.request.Request(
        telegram_api(method), data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        response.read()


def send_message(text: str, preview: bool = False) -> None:
    post_form("sendMessage", {
        "chat_id": env("TELEGRAM_CHAT_ID"), "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "false" if preview else "true",
    })


def send_photo(path: Path, caption: str) -> None:
    post_multipart("sendPhoto", {
        "chat_id": env("TELEGRAM_CHAT_ID"), "caption": caption[:1024], "parse_mode": "HTML",
    }, {"photo": path})


# ────────────────────────── 상태(signature) ──────────────────────────
def load_signature() -> str:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8")).get("signature", "")
    except Exception:
        return ""


def save_signature(signature: str, links: list[str]) -> None:
    STATE_PATH.write_text(
        json.dumps({
            "signature": signature,
            "links": links,
            "updated_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ────────────────────────── 페이지 처리 ──────────────────────────
def prepare_page(page: Page) -> None:
    page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        pass
    # 쿠키/광고동의(CMP)·팝업·광고·iframe을 DOM에서 완전히 제거한다(잔상 방지).
    # 주의: '[class*="ad"]' 같은 광범위 선택자는 body에 붙은 클래스(loaded 등)까지
    # 매칭해 구조를 부수므로 쓰지 않는다. html/body/head는 절대 삭제하지 않는다.
    page.evaluate(
        """
        () => {
          const KEEP = new Set(['HTML', 'BODY', 'HEAD']);
          const selectors = [
            'iframe',
            '[class*="advert"]', '[id*="advert"]',
            '[class*="ad-slot"]', '[class*="ad-unit"]', '[class*="ad-container"]',
            '[id^="google_ads"]', '[class*="dfp"]', '[class*="gpt-"]',
            '[class*="popup"]', '[class*="layer"]', '[class*="modal"]',
            '[class*="overlay"]', '[class*="backdrop"]', '[class*="veil"]',
            '[class*="cookie"]', '[id*="cookie"]',
            '[class*="consent"]', '[id*="consent"]',
            '[class*="cmp"]', '[class*="qc-cmp"]',
            '[class*="privacy"]', '[class*="gdpr"]',
            '[class*="onetrust"]', '[id*="onetrust"]',
            '[class*="sp_message"]', '[class*="sp-message"]', '[id*="sp_message"]',
            '[class*="message-container"]'
          ];
          for (const sel of selectors) {
            document.querySelectorAll(sel).forEach((node) => {
              if (node && !KEEP.has(node.tagName)
                  && node !== document.body && node !== document.documentElement) {
                node.remove();
              }
            });
          }
          // 스크롤 잠금 해제(모달이 body overflow:hidden 걸어둔 경우)
          if (document.documentElement) document.documentElement.style.overflow = '';
          if (document.body) document.body.style.overflow = '';
        }
        """
    )


def normalize_link(raw: str) -> str:
    link = (raw or "").strip().replace("&amp;", "&")
    if not link:
        return ""
    if link.startswith("//"):
        link = "https:" + link
    elif link.startswith("/"):
        link = "https://www.tradewindsnews.com" + link
    elif not re.match(r"^https?://", link, re.I):
        return ""
    if not re.match(r"^https?://(?:www\.)?tradewindsnews\.com/", link, re.I):
        return ""
    link = re.sub(r"^http://", "https://", link, flags=re.I)
    link = re.sub(r"^https://tradewindsnews\.com/", "https://www.tradewindsnews.com/", link, flags=re.I)
    link = link.split("#", 1)[0].split("?", 1)[0]
    return link.rstrip("/")


BLOCKED_TITLE_WORDS = ("subscribe", "login", "log in", "advertise",
                       "terms", "privacy", "cookie", "newsletter")
BLOCKED_PATH_PREFIXES = ("/archive", "/search", "/login", "/subscribe", "/about",
                         "/contact", "/privacy", "/terms", "/advertise",
                         "/newsletter", "/author", "/authors", "/topic", "/topics",
                         "/my-account")


def is_article(link: str, title: str) -> bool:
    if len(title) < 12:
        return False
    lowered = title.lower()
    if any(word in lowered for word in BLOCKED_TITLE_WORDS):
        return False
    path = re.sub(r"^https?://(?:www\.)?tradewindsnews\.com", "", link, flags=re.I).lower()
    if re.search(r"\.(?:jpg|jpeg|png|gif|svg|webp|pdf|css|js)$", path):
        return False
    if any(path == p or path.startswith(p + "/") for p in BLOCKED_PATH_PREFIXES):
        return False
    return len([s for s in path.split("/") if s]) >= 2


def extract_articles(page: Page, limit: int) -> list[dict[str, str]]:
    raw_items = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a[href]')).map((a) => {
          const rect = a.getBoundingClientRect();
          return {
            title: (a.innerText || a.textContent || '').trim(),
            href: a.getAttribute('href') || '',
            top: rect.top + window.scrollY,
            left: rect.left + window.scrollX
          };
        })
        """
    )
    seen: set[str] = set()
    articles: list[dict[str, str]] = []
    ordered = sorted(raw_items, key=lambda it: (float(it.get("top") or 0), float(it.get("left") or 0)))
    for item in ordered:
        title = re.sub(r"\s+", " ", str(item.get("title", ""))).strip()
        link = normalize_link(str(item.get("href", "")))
        if not link or not title or not is_article(link, title):
            continue
        if link in seen:
            continue
        seen.add(link)
        articles.append({"title": title, "link": link})
        if len(articles) >= limit:
            break
    return articles


def screenshot(page: Page, stamp: str) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"tradewinds_{stamp}.png"
    full = os.getenv("TW_FULL_PAGE", "false").lower() == "true"
    page.screenshot(path=str(path), full_page=full)
    return path


# ────────────────────────── 메시지 ──────────────────────────
def new_news_list_message(articles: list[dict[str, str]]) -> str:
    lines = [
        "📰 <b>TradeWinds 첫 페이지 기사 목록</b>", "",
        f"🔎 <b>검색어:</b> {html.escape(SEARCH_QUERY)}",
        f'🔗 <a href="{html.escape(SEARCH_URL)}">검색 화면 열기</a>', "",
    ]
    for i, a in enumerate(articles, 1):
        lines.append(f'{i}. <a href="{html.escape(a["link"])}">{html.escape(a["title"])}</a>')
    return "\n".join(lines)


def no_news_message(articles: list[dict[str, str]]) -> str:
    lines = [
        "ℹ️ <b>TradeWinds — 새 뉴스 없음</b>", "",
        f"🔎 <b>검색어:</b> {html.escape(SEARCH_QUERY)}",
        f'🔗 <a href="{html.escape(SEARCH_URL)}">검색 화면 열기</a>', "",
        "📰 <b>최근 뉴스 3개</b>",
    ]
    for i, a in enumerate(articles[:3], 1):
        lines.append(f'{i}. <a href="{html.escape(a["link"])}">{html.escape(a["title"])}</a>')
    return "\n".join(lines)


# ────────────────────────── 실행 ──────────────────────────
def in_quiet_hours() -> bool:
    hour = datetime.now(KST).hour
    return 1 <= hour < 5


def run_once() -> int:
    if in_quiet_hours() and os.getenv("TW_IGNORE_QUIET", "false").lower() != "true":
        print("Quiet hours (KST 01~05) → skip", flush=True)
        return 0

    checked_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    width = int(os.getenv("TW_VIEWPORT_WIDTH", "1280"))
    height = int(os.getenv("TW_VIEWPORT_HEIGHT", "900"))
    headless = os.getenv("TW_HEADLESS", "true").lower() != "false"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                locale="en-US", timezone_id="Asia/Seoul",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0.0.0 Safari/537.36"),
            )
            page = context.new_page()
            prepare_page(page)
            articles = extract_articles(page, ARTICLE_LIMIT)
            if not articles:
                raise RuntimeError("기사 링크를 추출하지 못했습니다(차단/구조변경 가능).")

            signature = "\n".join(sorted(a["link"] for a in articles))
            previous = load_signature()

            if signature == previous:
                # 변화 없음 → 캡쳐 없이 최근 3개 링크만
                send_message(no_news_message(articles))
                print(f"No change → 3 links only ({len(articles)} found)", flush=True)
            else:
                # 새 뉴스 → 캡쳐 + 목록
                shot = screenshot(page, stamp)
                caption = (f"🚢 <b>TradeWinds 검색 화면</b>\n"
                           f"🔎 <b>검색어:</b> {html.escape(SEARCH_QUERY)}  ·  {html.escape(checked_at)} KST")
                send_photo(shot, caption)
                send_message(new_news_list_message(articles))
                save_signature(signature, [a["link"] for a in articles])
                print(f"New news → photo + list ({len(articles)} found)", flush=True)
            page.close()
        finally:
            browser.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture TradeWinds search and send to Telegram.")
    parser.add_argument("--once", action="store_true", help="Run one check and exit.")
    parser.parse_args()
    load_dotenv(BASE_DIR / ".env")
    try:
        return run_once()
    except (urllib.error.URLError, RuntimeError, PlaywrightTimeoutError) as exc:
        msg = f"⚠️ <b>TradeWinds 봇 오류</b>\n{html.escape(str(exc))}"
        print(msg, file=sys.stderr, flush=True)
        if os.getenv("TELEGRAM_NOTIFY_ERRORS", "false").lower() == "true":
            try:
                send_message(msg)
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
