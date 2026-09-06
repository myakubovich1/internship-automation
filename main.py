import base64
import html
import json
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import requests
from instagrapi import Client

TARGET_USERNAME = os.getenv("TARGET_USERNAME", "zero2sudo")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
STATE_PATH = Path(__file__).with_name("state.json")
MAX_SEEN_IDS = 500


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"seen_story_ids": []}
    try:
        data = json.loads(STATE_PATH.read_text())
        if not isinstance(data, dict):
            raise ValueError("state is not a JSON object")
        data.setdefault("seen_story_ids", [])
        return data
    except Exception:
        return {"seen_story_ids": []}


def save_state(seen_ids: list[str]) -> None:
    deduped = list(dict.fromkeys(seen_ids))[-MAX_SEEN_IDS:]
    STATE_PATH.write_text(
        json.dumps(
            {
                "seen_story_ids": deduped,
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n"
    )


def build_client() -> Client:
    return Client(public_transport="curl", public_transport_impersonate="chrome136")


def fetch_stories(client: Client):
    try:
        user_id = client.user_id_from_username(TARGET_USERNAME)
        return client.user_stories(user_id)
    except Exception as public_error:
        session_id = os.getenv("IG_SESSIONID", "").strip()
        if not session_id:
            raise RuntimeError(
                "Anonymous Instagram story fetch failed. Add IG_SESSIONID as a "
                "GitHub Secret to enable authenticated fallback."
            ) from public_error

        authed = Client(public_transport="curl", public_transport_impersonate="chrome136")
        authed.login_by_sessionid(session_id)
        user_id = authed.user_id_from_username(TARGET_USERNAME)
        return authed.user_stories(user_id)


def story_metadata(story) -> dict:
    story_id = str(getattr(story, "pk", "") or getattr(story, "id", ""))
    taken_at = getattr(story, "taken_at", None)
    if taken_at:
        try:
            taken_at_text = taken_at.astimezone(timezone.utc).isoformat()
        except Exception:
            taken_at_text = str(taken_at)
    else:
        taken_at_text = ""

    links = []
    for item in getattr(story, "links", []) or []:
        uri = getattr(item, "webUri", None)
        if uri:
            links.append(str(uri))

    return {
        "story_id": story_id,
        "taken_at_utc": taken_at_text,
        "instagram_url": f"https://www.instagram.com/stories/{TARGET_USERNAME}/{story_id}/",
        "link_stickers": links,
        "thumbnail_url": str(getattr(story, "thumbnail_url", "") or ""),
    }


def download_thumbnail(url: str) -> tuple[str, str]:
    if not url:
        raise RuntimeError("Story has no thumbnail URL")
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    response.raise_for_status()
    mime = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    return mime, base64.b64encode(response.content).decode("ascii")


def parse_json_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


def analyze_with_gemini(stories_with_images: list[tuple[dict, str, str]]) -> dict:
    api_key = require_env("GEMINI_API_KEY")

    prompt = """
You are analyzing a batch of NEW Instagram Stories from @zero2sudo, an account
that posts tech internships, early-career jobs, recruiting events, application
openings, deadlines, and recruiting updates.

The images are in chronological order. Consecutive slides may describe the same
opportunity, so merge those slides into one opportunity instead of duplicating it.

Return ONLY valid JSON with this exact top-level shape:
{
  "digest_summary": "one short sentence about what was posted",
  "opportunities": [
    {
      "company": "",
      "role_or_program": "",
      "opportunity_type": "internship|new_grad|early_career|event|application_update|recruiting_update|other",
      "season_year": "",
      "location": "",
      "deadline": "",
      "application_url": "",
      "summary": "",
      "urgency": "high|normal|low",
      "source_story_ids": [""]
    }
  ],
  "other_updates": [
    {
      "summary": "",
      "source_story_ids": [""]
    }
  ]
}

Rules:
- Be concise and factual.
- Do not invent company names, roles, dates, deadlines, or URLs.
- Prefer a provided link sticker as application_url when it appears relevant.
- Put genuine internship / early-career opportunities and recruiting updates in
  "opportunities".
- Put unrelated or general content in "other_updates".
- If a field is not visible or inferable from the story, use an empty string.
- "high" urgency means an application just opened, is reopening, has a near
  deadline, limited availability, or the story explicitly urges immediate action.
""".strip()

    parts = [{"text": prompt}]
    for metadata, mime, image_b64 in stories_with_images:
        parts.append(
            {
                "text": (
                    "\nSTORY METADATA:\n"
                    + json.dumps(
                        {
                            "story_id": metadata["story_id"],
                            "taken_at_utc": metadata["taken_at_utc"],
                            "instagram_url": metadata["instagram_url"],
                            "link_stickers": metadata["link_stickers"],
                        },
                        ensure_ascii=False,
                    )
                )
            }
        )
        parts.append({"inline_data": {"mime_type": mime, "data": image_b64}})

    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    response = requests.post(
        endpoint,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=90,
    )
    response.raise_for_status()
    data = response.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return parse_json_response(text)


def build_email_html(digest: dict, story_count: int) -> str:
    opportunities = digest.get("opportunities") or []
    other_updates = digest.get("other_updates") or []
    summary = html.escape(digest.get("digest_summary") or f"{story_count} new stories.")

    chunks = [
        "<html><body>",
        f"<h2>@{html.escape(TARGET_USERNAME)} — {story_count} new stor{'y' if story_count == 1 else 'ies'}</h2>",
        f"<p>{summary}</p>",
    ]

    if opportunities:
        chunks.append("<h3>Opportunities</h3>")
        for item in opportunities:
            company = html.escape(item.get("company") or "Unknown company")
            role = html.escape(item.get("role_or_program") or "Opportunity")
            opp_type = html.escape(item.get("opportunity_type") or "")
            season = html.escape(item.get("season_year") or "")
            location = html.escape(item.get("location") or "")
            deadline = html.escape(item.get("deadline") or "")
            summary_text = html.escape(item.get("summary") or "")
            urgency = html.escape((item.get("urgency") or "normal").upper())
            application_url = item.get("application_url") or ""

            chunks.append(f"<p><strong>{company} — {role}</strong><br>")
            details = [x for x in [opp_type, season, location] if x]
            if details:
                chunks.append(" · ".join(details) + "<br>")
            if deadline:
                chunks.append(f"Deadline: {deadline}<br>")
            chunks.append(f"Urgency: {urgency}<br>")
            if summary_text:
                chunks.append(f"{summary_text}<br>")
            if application_url:
                safe_url = html.escape(application_url, quote=True)
                chunks.append(f'<a href="{safe_url}">Open application/link</a>')
            chunks.append("</p>")

    if other_updates:
        chunks.append("<h3>Other updates</h3><ul>")
        for item in other_updates:
            chunks.append(f"<li>{html.escape(item.get('summary') or '')}</li>")
        chunks.append("</ul>")

    chunks.append(
        f'<p><a href="https://www.instagram.com/{html.escape(TARGET_USERNAME)}/">'
        "Open Instagram profile</a></p>"
    )
    chunks.append("</body></html>")
    return "".join(chunks)


def send_email(digest: dict, story_count: int) -> None:
    email_from = require_env("EMAIL_FROM")
    email_to = require_env("EMAIL_TO")
    app_password = require_env("GMAIL_APP_PASSWORD").replace(" ", "")

    opportunities = digest.get("opportunities") or []
    high_urgency = sum(
        1 for item in opportunities if (item.get("urgency") or "").lower() == "high"
    )
    prefix = "🚨" if high_urgency else "📬"

    msg = EmailMessage()
    msg["From"] = email_from
    msg["To"] = email_to
    msg["Subject"] = (
        f"{prefix} zero2sudo: {len(opportunities)} "
        f"{'opportunity' if len(opportunities) == 1 else 'opportunities'} / "
        f"{story_count} new stories"
    )
    msg.set_content(
        (digest.get("digest_summary") or f"{story_count} new zero2sudo stories.")
        + "\n\nOpen the HTML version of this email for details."
    )
    msg.add_alternative(build_email_html(digest, story_count), subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(email_from, app_password)
        server.send_message(msg)


def main() -> int:
    for required in ("GEMINI_API_KEY", "EMAIL_FROM", "EMAIL_TO", "GMAIL_APP_PASSWORD"):
        require_env(required)

    state = load_state()
    seen_list = [str(x) for x in state.get("seen_story_ids", [])]
    seen = set(seen_list)

    client = build_client()
    stories = fetch_stories(client)

    metadata_by_id = {}
    for story in stories:
        meta = story_metadata(story)
        if meta["story_id"]:
            metadata_by_id[meta["story_id"]] = meta

    active_ids = list(metadata_by_id.keys())
    new_ids = [story_id for story_id in active_ids if story_id not in seen]

    if not new_ids:
        print("No new stories.")
        return 0

    new_ids.sort(key=lambda story_id: metadata_by_id[story_id].get("taken_at_utc") or "")
    stories_with_images = []
    failed_images = []

    for story_id in new_ids:
        meta = metadata_by_id[story_id]
        try:
            mime, image_b64 = download_thumbnail(meta["thumbnail_url"])
            stories_with_images.append((meta, mime, image_b64))
        except Exception as exc:
            failed_images.append((story_id, str(exc)))

    if not stories_with_images:
        raise RuntimeError(f"Could not download any new story thumbnails: {failed_images}")

    digest = analyze_with_gemini(stories_with_images)
    if failed_images:
        digest.setdefault("other_updates", []).append(
            {
                "summary": f"{len(failed_images)} new stories could not be analyzed.",
                "source_story_ids": [story_id for story_id, _ in failed_images],
            }
        )

    send_email(digest, len(new_ids))
    save_state(seen_list + new_ids)
    print(f"Sent digest for {len(new_ids)} new stories.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
