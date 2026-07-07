"""Weekly Trello board summarizer — Maehwa Holdings letter format.

Fetches Trello cards that changed in the last 7 days, summarizes them as a
formal business letter, and creates a PDF in the configured Google Drive folder.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from dotenv import load_dotenv
from fpdf import FPDF
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

load_dotenv(".env")

# Trello settings
TRELLO_KEY = os.environ["TRELLO_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
TRELLO_BOARD_ID = os.environ["TRELLO_BOARD_ID"]

# LLM settings
LLM_API_KEY = os.environ["LLM_API_KEY"]
LLM_URL = os.environ["LLM_URL"]
LLM_MODEL = os.environ["LLM_MODEL"]

# Google Drive settings
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN")

# Letter configuration
SENDER_NAME = os.environ.get("SENDER_NAME", "Trello Assistant")
RECIPIENT_NAME = os.environ.get("RECIPIENT_NAME", "Adam Toth-Fejel")
RECIPIENT_ADDRESS = os.environ.get(
    "RECIPIENT_ADDRESS", "1953 North 6th Street\nConcord, CA 94519"
)
LOGO_PATH = os.environ.get("LOGO_PATH", "logo-clear-bg-cropped.png")
PDF_MARGIN_LEFT = float(os.environ.get("PDF_MARGIN_LEFT", "25.4"))  # 1 inch in mm
PDF_MARGIN_RIGHT = float(os.environ.get("PDF_MARGIN_RIGHT", "25.4"))
PDF_MARGIN_TOP = float(os.environ.get("PDF_MARGIN_TOP", "25.4"))
PDF_MARGIN_BOTTOM = float(os.environ.get("PDF_MARGIN_BOTTOM", "25.4"))

DAYS_BACK = 7

SCOPES = ["https://www.googleapis.com/auth/drive"]


def get_google_creds():
    """Authenticate using OAuth refresh token or fall back to service account."""
    if GOOGLE_REFRESH_TOKEN and GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        return UserCredentials(
            token=None,
            refresh_token=GOOGLE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=SCOPES,
        )
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    raise RuntimeError(
        "No Google credentials found. Set GOOGLE_REFRESH_TOKEN + GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET, "
        "or GOOGLE_SERVICE_ACCOUNT_JSON."
    )


def is_gemini() -> bool:
    return "googleapis.com" in LLM_URL


async def fetch_trello_lists() -> dict[str, dict]:
    url = (
        f"https://api.trello.com/1/boards/{TRELLO_BOARD_ID}/lists"
        f"?key={TRELLO_KEY}&token={TRELLO_TOKEN}"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Trello lists API error: {resp.status}")
            lists = await resp.json()
    return {lst["id"]: lst for lst in lists}


async def fetch_trello_cards() -> list[dict]:
    url = (
        f"https://api.trello.com/1/boards/{TRELLO_BOARD_ID}/cards"
        f"?key={TRELLO_KEY}&token={TRELLO_TOKEN}"
        f"&members=true&member_fields=fullName"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Trello API error: {resp.status}")
            cards = await resp.json()

    list_map = await fetch_trello_lists()
    for card in cards:
        card["list"] = list_map.get(card.get("idList"), {"name": "Unknown list"})
    return cards


def format_trello_cards(cards: list[dict]) -> str:
    lines = []
    for card in cards:
        name = card.get("name", "Untitled")
        desc = card.get("desc", "").strip()
        list_name = card.get("list", {}).get("name", "Unknown list")
        due = card.get("due")
        last_activity = card.get("dateLastActivity", "")
        members = ", ".join(m.get("fullName", "") for m in card.get("members", []))

        lines.append(f"Card: {name}")
        lines.append(f"Current list: {list_name}")
        if last_activity:
            lines.append(f"Last activity: {last_activity}")
        if due:
            lines.append(f"Due: {due}")
        if members:
            lines.append(f"Members: {members}")
        if desc:
            lines.append(f"Description:\n{desc}")
        lines.append("")
    return "\n".join(lines)


def build_prompt(cards_text: str) -> str:
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    since = (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).strftime("%A, %B %d, %Y")
    return (
        f"Today is {today}. You are a helpful executive assistant named Trello Assistant. "
        f"Write a weekly update letter for Adam Toth-Fejel summarizing changes on the Trello board from {since} to today.\n\n"
        "Write from a third-person perspective, as if you are the assistant reporting on what Malachy has been working on. "
        "Do not use first person ('I', 'we', 'my'). Do not say 'this card was moved' or list individual Trello cards. "
        "Instead, group related work into themes and explain what progress was made, what was completed, and what remains in flight. "
        "Keep the letter body short enough to fit on one printed page: about 250–350 words. "
        "Use a calm, professional tone. Keep it concise but informative.\n\n"
        "If there are completed items, describe them as done. If there are ongoing items, describe them as in progress. "
        "If something needs Adam's attention or input, mention it.\n\n"
        f"Cards data:\n\n{cards_text}\n\n"
        "Write the letter body only, as several paragraphs. Do not include a salutation, closing, or signature."
    )


def gemini_payload(prompt: str) -> dict:
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 4096},
    }


def openai_payload(prompt: str) -> dict:
    return {
        "model": LLM_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a helpful executive assistant writing a weekly business letter "
                    "about Trello board activity. Use professional prose paragraphs. Be accurate."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.5,
        "max_tokens": 4096,
    }


async def summarize(prompt: str) -> str:
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    if is_gemini():
        payload = gemini_payload(prompt)
        url = f"{LLM_URL}?key={LLM_API_KEY}"
        headers.pop("Authorization", None)
    else:
        payload = openai_payload(prompt)
        url = LLM_URL

    last_error = None
    for attempt in range(3):
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                data = await resp.json()
                if resp.status == 200:
                    if is_gemini():
                        return data["candidates"][0]["content"]["parts"][0]["text"]
                    return data["choices"][0]["message"]["content"]
                last_error = f"LLM API error {resp.status}: {json.dumps(data)}"
                print(f"Attempt {attempt + 1} failed: {last_error}")
                await asyncio.sleep(2 ** attempt)

    raise RuntimeError(last_error)


def filter_recent_cards(cards: list[dict]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    recent = []
    for card in cards:
        last = card.get("dateLastActivity")
        if not last:
            continue
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            continue
        if last_dt >= cutoff:
            recent.append(card)
    return recent


class MaehwaLetterPDF(FPDF):
    def header(self):
        # Only show logo on the first page.
        if self.page_no() != 1:
            return

        # Logo at top left; afterwards force cursor below the logo so body
        # text does not wrap beside it.
        if os.path.exists(LOGO_PATH):
            self.image(LOGO_PATH, x=PDF_MARGIN_LEFT, y=PDF_MARGIN_TOP, w=35)
        # Reset to left margin and move y below the largest expected logo height.
        self.set_x(PDF_MARGIN_LEFT)
        self.set_y(PDF_MARGIN_TOP + 35)
        self.ln(8)


def create_pdf(summary: str, output_path: str) -> None:
    # Replace curly quotes and other non-Latin-1 chars with ASCII equivalents.
    summary = (
        summary
        .replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("—", "--")
        .replace("–", "-")
        .replace("…", "...")
    )

    pdf = MaehwaLetterPDF("P", "mm", "Letter")
    pdf.set_auto_page_break(auto=True, margin=PDF_MARGIN_BOTTOM)
    pdf.set_margins(PDF_MARGIN_LEFT, PDF_MARGIN_TOP, PDF_MARGIN_RIGHT)
    pdf.add_page()

    # Date
    pdf.set_font("Times", "", 12)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 6, datetime.now().strftime("%B %d, %Y"), new_x="LMARGIN", new_y="NEXT", align="L")
    pdf.ln(8)

    # Salutation
    pdf.cell(0, 6, "Dear Adam,", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # Body paragraphs
    pdf.set_font("Times", "", 12)
    for paragraph in summary.split("\n\n"):
        text = paragraph.strip()
        if not text:
            continue
        pdf.multi_cell(0, 6, text)
        pdf.ln(4)

    # Closing
    pdf.ln(6)
    pdf.cell(0, 6, "Sincerely,", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.cell(0, 6, SENDER_NAME, new_x="LMARGIN", new_y="NEXT")

    pdf.output(output_path)


def upload_pdf_to_drive(pdf_path: str, title: str) -> str:
    creds = get_google_creds()
    drive_service = build("drive", "v3", credentials=creds)

    file_metadata = {
        "name": title,
        "mimeType": "application/pdf",
    }
    if GOOGLE_DRIVE_FOLDER_ID:
        file_metadata["parents"] = [GOOGLE_DRIVE_FOLDER_ID]

    media = MediaFileUpload(pdf_path, mimetype="application/pdf")
    file = (
        drive_service.files()
        .create(body=file_metadata, media_body=media, supportsAllDrives=True, fields="id, webViewLink")
        .execute()
    )
    return file.get("webViewLink", f"https://drive.google.com/file/d/{file['id']}/view")


async def main() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    pdf_path = f"/tmp/trello_weekly_summary_{today}.pdf"

    cards = await fetch_trello_cards()
    if not cards:
        print("No cards found on board.")
        return

    recent_cards = filter_recent_cards(cards)
    if not recent_cards:
        print("No card changes in the last 7 days.")
        return

    cards_text = format_trello_cards(recent_cards)
    prompt = build_prompt(cards_text)
    summary = await summarize(prompt)

    create_pdf(summary, pdf_path)
    print(f"Created PDF: {pdf_path}")

    pdf_title = f"Weekly Trello Summary - {today}.pdf"
    pdf_url = upload_pdf_to_drive(pdf_path, pdf_title)
    print(f"Uploaded PDF: {pdf_url}")

    # Clean up local file
    os.remove(pdf_path)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
