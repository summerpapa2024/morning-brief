"""
Morning Brief — daily news digest agent.
Run directly: python main.py
Scheduled: Windows Task Scheduler triggers this at 7 AM KST daily.
"""

import os
import sys
import smtplib
import logging
import textwrap
import unicodedata
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import feedparser
from openai import OpenAI
from jinja2 import Template
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False))],
)
log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
SEND_LIMIT = 3  # max emails per run (guards against retry loops)
_sent_count = 0

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
GMAIL_USER = os.environ["GMAIL_USER"]          # your Gmail address
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ.get("EMAIL_TO", GMAIL_USER)

FEEDS = {
    "🌍 국제 정치·경제": [
        "http://feeds.bbci.co.uk/news/world/rss.xml",
        "http://feeds.bbci.co.uk/news/business/rss.xml",
        "https://feeds.reuters.com/reuters/worldNews",
        "https://feeds.reuters.com/reuters/businessNews",
    ],
    "💄 뷰티·패션 시장": [
        "https://wwd.com/feed/",                                          # WWD — 패션+뷰티 비즈니스
        "https://www.glossy.co/feed",                                     # Glossy — 뷰티·패션 시장 분석
        "https://www.cosmeticsdesign.com/rss/feed",                       # CosmeticsDesign — 원료·시장
        "https://cosmeticsbusiness.com/rss/articles",                     # CosmeticsBusiness — 업계 동향
    ],
    "🤖 AI·기술": [
        "https://techcrunch.com/category/artificial-intelligence/feed/",
        "https://www.technologyreview.com/feed/",
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "https://venturebeat.com/category/ai/feed/",
    ],
}

# Per-theme focus instructions appended to the GPT user prompt
THEME_FOCUS = {
    "💄 뷰티·패션 시장": (
        "화장품·패션 브랜드의 시장 동향, M&A, 실적, 소비자 트렌드, 유통 채널 변화에 집중하세요. "
        "제품 리뷰나 뷰티 팁 같은 라이프스타일 기사는 제외하세요."
    ),
}

SYSTEM_PROMPT = textwrap.dedent("""
    당신은 한국어로 일간 뉴스 브리핑을 작성하는 전문 에디터입니다.

    [1단계 — 기사 요약]
    - 정확히 5개의 기사를 작성하세요. 더도 말고 덜도 말고.
    - 각 기사는 다음 형식으로 작성하세요:
      **[한글 제목]**
      [3~4문장 한국어 요약: 무슨 일이 있었는지, 왜 중요한지, 앞으로 어떻게 될지]
      [출처:N]
      (N은 해당 기사의 원본 번호, 예: [출처:3])
    - 각 기사는 빈 줄로 구분하세요.

    [2단계 — 인사이트]
    5개 기사를 모두 작성한 뒤, 반드시 아래 구분선을 삽입하고 인사이트를 작성하세요:

    ---인사이트---
    [2~3문단 분석]
    - 1문단: 오늘 기사들을 관통하는 큰 흐름 또는 공통된 신호
    - 2문단: 이 흐름을 어떤 시각으로 읽어야 하는지 — 낙관적 해석과 비관적 해석 모두 제시
    - 3문단(선택): 앞으로 2~4주 안에 주목해야 할 지표나 사건

    공통 규칙:
    - 전체를 한국어로 작성하세요. 브랜드명은 원문 그대로 보존하세요 (올리브영, Amorepacific, Shiseido, OpenAI 등).
    - '획기적인', '혁신적인', '중요한', '주목할만한', '역대급' 같은 과장 형용사는 사용하지 마세요.
    - 서론이나 설명 없이 바로 기사부터 시작하세요.
""").strip()

EMAIL_TEMPLATE = Template("""
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body { font-family: -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", Georgia, serif;
         background:#f4f4f2; margin:0; padding:0; color:#222; }
  .wrapper { max-width:640px; margin:0 auto; background:#ffffff; }
  .header { background:#111111; color:#ffffff; padding:32px 36px 24px; }
  .header h1 { margin:0; font-size:24px; font-weight:600; letter-spacing:-0.3px; }
  .header .date { margin:8px 0 0; font-size:13px; color:#888888; font-family:sans-serif; }
  .section { padding:28px 36px 20px; border-bottom:2px solid #f0f0ee; }
  .section:last-of-type { border-bottom:none; }
  .section-title { font-size:13px; font-weight:700; font-family:sans-serif;
                   color:#888888; letter-spacing:1px; text-transform:uppercase;
                   margin:0 0 20px; }
  .article { margin-bottom:20px; padding-bottom:20px; border-bottom:1px solid #f0f0ee; }
  .article:last-child { border-bottom:none; margin-bottom:0; padding-bottom:0; }
  .article-title { font-size:15px; font-weight:700; color:#111111;
                   margin:0 0 8px; line-height:1.5; }
  .article-body { font-size:14px; line-height:1.8; color:#444444; margin:0 0 8px; }
  .article-link { font-size:12px; font-family:sans-serif; }
  .article-link a { color:#555555; text-decoration:none; border-bottom:1px solid #dddddd; }
  .insight { margin-top:24px; padding:20px 22px; background:#f7f6f2;
             border-left:3px solid #bbbbaa; border-radius:2px; }
  .insight-label { font-size:11px; font-weight:700; font-family:sans-serif;
                   color:#888877; letter-spacing:1px; text-transform:uppercase;
                   margin:0 0 10px; }
  .insight-body { font-size:14px; line-height:1.85; color:#333322; margin:0; }
  .insight-body p { margin:0 0 12px; }
  .insight-body p:last-child { margin:0; }
  .no-news { font-size:13px; color:#999999; font-style:italic; }
  .footer { padding:20px 36px 28px; background:#f4f4f2; }
  .footer p { font-size:12px; color:#aaaaaa; font-family:sans-serif; margin:0; line-height:1.7; }
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <h1>모닝 브리프</h1>
    <div class="date">{{ date_str }}</div>
  </div>
  {% for section_title, articles, insight in sections %}
  <div class="section">
    <div class="section-title">{{ section_title }}</div>
    {% if articles %}
      {% for text, url in articles %}
      <div class="article">
        {% set lines = text.split("\\n") %}
        {% set title_line = lines[0].strip().lstrip("*#").rstrip("*").strip() %}
        {% set body_lines = lines[1:] %}
        <div class="article-title">{{ title_line }}</div>
        <p class="article-body">{{ body_lines | join(" ") | trim }}</p>
        {% if url %}<div class="article-link"><a href="{{ url }}">원문 읽기 →</a></div>{% endif %}
      </div>
      {% endfor %}
      {% if insight %}
      <div class="insight">
        <div class="insight-label">에디터 인사이트</div>
        <div class="insight-body">
          {% for para in insight.split("\\n\\n") %}
          {% if para.strip() %}<p>{{ para.strip() }}</p>{% endif %}
          {% endfor %}
        </div>
      </div>
      {% endif %}
    {% else %}
    <p class="no-news">오늘 새로운 소식이 없습니다.</p>
    {% endif %}
  </div>
  {% endfor %}
  <div class="footer">
    <p>{{ delivery_time }} KST 발송 &nbsp;·&nbsp; Morning Brief Agent</p>
    {% if warnings %}<p>⚠ {{ warnings }}</p>{% endif %}
  </div>
</div>
</body>
</html>
""")


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

def fetch_items(urls: list[str], window_hours: int = 36) -> list[dict]:
    """Fetch RSS items published within the last window_hours hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    items = []
    seen_paths = set()

    for url in urls:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "MorningBrief/1.0"})
            entries = feed.entries[:15]
        except Exception as exc:
            log.warning("Feed error %s: %s", url, exc)
            continue

        for entry in entries:
            # --- date ---
            pub = (
                entry.get("published_parsed")
                or entry.get("updated_parsed")
                or entry.get("dc_date_parsed")
            )
            if pub:
                import calendar
                pub_dt = datetime.fromtimestamp(calendar.timegm(pub), tz=timezone.utc)
                if pub_dt < cutoff:
                    continue
            # if no date field at all, include anyway (better than silent drop)

            # --- dedup by URL path ---
            link = entry.get("link", "")
            try:
                from urllib.parse import urlparse
                path = urlparse(link).path.rstrip("/")
            except Exception:
                path = link
            if path and path in seen_paths:
                continue
            if path:
                seen_paths.add(path)

            title = entry.get("title", "").strip()
            summary = entry.get("summary", entry.get("description", "")).strip()
            # strip HTML tags naively
            import re
            summary = re.sub(r"<[^>]+>", "", summary)[:300]

            if title:
                items.append({"title": title, "summary": summary, "url": link})

    return items


def dedup_by_title(items: list[dict]) -> list[dict]:
    """Remove near-duplicate titles using token Jaccard similarity."""
    STOPWORDS = {"the", "a", "an", "of", "in", "and", "to", "for", "on", "at", "by", "with", "is", "are"}

    def tokens(text):
        import re
        words = re.findall(r"[a-z]+", text.lower())
        return set(w for w in words if w not in STOPWORDS and len(w) > 2)

    kept = []
    kept_tokens = []
    for item in items:
        t = tokens(item["title"])
        if not t:
            kept.append(item)
            kept_tokens.append(t)
            continue
        duplicate = False
        for kt in kept_tokens:
            if kt:
                overlap = len(t & kt) / min(len(t), len(kt))
                if overlap > 0.8:
                    duplicate = True
                    break
        if not duplicate:
            kept.append(item)
            kept_tokens.append(t)
    return kept


# ---------------------------------------------------------------------------
# Summarizer
# ---------------------------------------------------------------------------

def summarize(theme: str, items: list[dict]) -> tuple[list[tuple[str, str]], str]:
    """Return (articles, insight) where articles is a list of (text, url) tuples."""
    if not items:
        return [], ""

    client = OpenAI(api_key=OPENAI_API_KEY)

    capped = items[:15]
    numbered = "\n".join(
        f"{i+1}. {it['title']} — {it['summary']}" for i, it in enumerate(capped)
    )
    focus = THEME_FOCUS.get(theme, "")
    focus_line = f"\n\n추가 지시: {focus}" if focus else ""
    user_prompt = f"주제: {theme}\n오늘의 헤드라인과 요약:\n{numbered}{focus_line}\n\n가장 중요한 5개 기사를 작성하고, 인사이트를 추가하세요."

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=2000,
            temperature=0.3,
        )
        raw = response.choices[0].message.content.strip()
    except Exception as exc:
        log.error("OpenAI error for %s: %s", theme, exc)
        return [(it["title"][:120], it["url"]) for it in capped[:5]], ""

    import re

    # Split articles section and insight section
    parts = re.split(r"\n---인사이트---\n", raw, maxsplit=1)
    articles_raw = parts[0]
    insight = parts[1].strip() if len(parts) > 1 else ""

    articles = []
    for block in articles_raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        url = ""
        m = re.search(r"\[출처:(\d+)\]", block)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(capped):
                url = capped[idx]["url"]
            block = re.sub(r"\s*\[출처:\d+\]", "", block).strip()
        articles.append((block, url))
        if len(articles) == 5:
            break

    return articles, insight


# ---------------------------------------------------------------------------
# Emailer
# ---------------------------------------------------------------------------

def send_email(subject: str, html_body: str, is_confirmation: bool = False) -> bool:
    global _sent_count
    if _sent_count >= SEND_LIMIT:
        log.warning("Send limit (%d) reached — skipping", SEND_LIMIT)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            smtp.send_message(msg)
        _sent_count += 1
        log.info("Email sent: %s", subject)
        return True
    except Exception as exc:
        log.error("SMTP error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now_kst = datetime.now(KST)
    date_str = now_kst.strftime("%A, %B %-d, %Y") if os.name != "nt" else now_kst.strftime("%A, %B %#d, %Y")
    delivery_time = now_kst.strftime("%H:%M")

    log.info("Starting Morning Brief for %s", date_str)

    sections = []
    warnings = []

    for theme, feed_urls in FEEDS.items():
        log.info("Fetching: %s", theme)
        items = fetch_items(feed_urls)
        items = dedup_by_title(items)
        log.info("  %d items after dedup", len(items))

        if not items:
            warnings.append(f"{theme}: 소식 없음")
            sections.append((theme, [], ""))
            continue

        articles, insight = summarize(theme, items)
        sections.append((theme, articles, insight))

    html = EMAIL_TEMPLATE.render(
        date_str=date_str,
        delivery_time=delivery_time,
        sections=sections,
        warnings=" | ".join(warnings) if warnings else "",
    )

    day_fmt = "%#d" if os.name == "nt" else "%-d"
    subject = f"Morning Brief — {now_kst.strftime('%a %b ' + day_fmt)}"
    ok = send_email(subject, html)

    if ok:
        # confirmation email
        confirm_html = f"<p style='font-family:sans-serif;color:#555'>Brief delivered at <b>{delivery_time} KST</b> on {date_str}.</p>"
        send_email(f"✓ Morning Brief sent — {now_kst.strftime('%b ' + day_fmt)}", confirm_html, is_confirmation=True)
        log.info("Done.")
    else:
        log.error("Brief failed to send.")
        sys.exit(1)


if __name__ == "__main__":
    main()
