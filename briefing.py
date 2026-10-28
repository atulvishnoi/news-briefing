"""Daily news briefing from Google News (India edition), sent by email."""
import argparse
import calendar
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import anthropic
import feedparser
import markdown
from dotenv import load_dotenv

load_dotenv()

BASE = "https://news.google.com/rss"
EDITION = "hl=en-IN&gl=IN&ceid=IN:en"  # India edition, English
FEEDS = {
    "AI": [f"{BASE}/search?q=%22artificial+intelligence%22+OR+OpenAI+OR+Anthropic+OR+Gemini+when:1d&{EDITION}"],
    "World": [f"{BASE}/headlines/section/topic/WORLD?{EDITION}"],
    "India": [f"{BASE}/headlines/section/topic/NATION?{EDITION}", f"{BASE}?{EDITION}"],
}
ITEMS_PER_FEED = 30
IST = timezone(timedelta(hours=5, minutes=30))


def fetch(urls: list[str], since: datetime) -> list[dict]:
    """Headlines from the given feeds published after `since`, without duplicates."""
    items, seen_titles = [], set()
    for url in urls:
        for entry in feedparser.parse(url).entries[:ITEMS_PER_FEED]:
            if entry.get("published_parsed"):
                published = datetime.fromtimestamp(calendar.timegm(entry.published_parsed), timezone.utc)
                if published < since:
                    continue
            if entry.title in seen_titles:
                continue
            seen_titles.add(entry.title)
            items.append({"title": entry.title, "url": entry.link})
    return items


SYSTEM_PROMPT = """You write a morning news briefing for one reader in India.
You receive Google News headlines in three sections: AI, World, and India.
For each section:
- Merge headlines about the same story and pick the 5 most important stories.
- Use web search to read up on each chosen story, so the summary rests on facts, not only the headline.
- Write each story as a bold headline on its own line, then a summary of about 5 lines
  (4 to 5 plain sentences: what happened, who is involved, key numbers or dates, and why it matters),
  then a [Read more](url) link using the URL given with the headline.
Use a "## " heading per section. If a section has nothing important, say so in one line.
Only state facts found in the headlines or your search results; do not guess."""

WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 20}


def summarize(sections: dict[str, list[dict]]) -> str:
    """Claude's briefing in Markdown, or a plain headline list without a Claude key."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "\n\n".join(
            f"## {name}\n\n" + ("\n".join(f"- [{i['title']}]({i['url']})" for i in items[:10]) or "No new stories.")
            for name, items in sections.items()
        )
    headlines = "\n\n".join(
        f"# {name}\n" + "\n".join(f"- {i['title']} ({i['url']})" for i in items)
        for name, items in sections.items()
    )
    client = anthropic.Anthropic()
    question = {"role": "user", "content": headlines}
    content = []  # assistant blocks so far, kept across pause_turn resumes
    for _ in range(5):
        messages = [question] + ([{"role": "assistant", "content": content}] if content else [])
        response = client.beta.messages.create(
            model="claude-sonnet-5-5",
            max_tokens=16000,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_PROMPT,
            tools=[WEB_SEARCH],
            messages=messages,
        )
        content += response.content
        if response.stop_reason != "pause_turn":  # long search runs pause; resend to continue
            break
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write today's briefing")
    # The briefing is the text written after the last search.
    last_search = max((n for n, b in enumerate(content) if b.type.endswith("tool_result")), default=-1)
    return "".join(b.text for b in content[last_search + 1:] if b.type == "text")


def send_email(subject: str, body_md: str) -> None:
    """Send the briefing through Gmail as HTML with a plain-text fallback."""
    sender = os.environ["EMAIL_FROM"]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = os.environ["EMAIL_TO"]  # one address, or several separated by commas
    msg.set_content(body_md)
    msg.add_alternative(markdown.markdown(body_md), subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, os.environ["GMAIL_APP_PASSWORD"])
        smtp.send_message(msg)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send the news briefing by email.")
    parser.add_argument("--hours", type=int, default=24, help="how far back to look (default 24)")
    parser.add_argument("--dry-run", action="store_true", help="print the briefing instead of emailing it")
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    sections = {name: fetch(urls, since) for name, urls in FEEDS.items()}
    briefing = summarize(sections)
    subject = f"Daily News briefing, {datetime.now(IST):%d %b %Y, %I:%M %p} IST"

    if args.dry_run:
        print(subject, "\n\n", briefing)
    else:
        send_email(subject, briefing)
        print(f"Sent: {subject}")


if __name__ == "__main__":
    main()
