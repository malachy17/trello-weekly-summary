"""Weekly Trello board summarizer.

Fetches cards from a Trello board, summarizes them using an LLM,
creates a new Google Doc in a Drive folder, and optionally creates
a matching PDF.
"""

import json
import os
from datetime import datetime
from typing import Any

import aiohttp
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Trello settings
TRELLO_KEY = os.environ["TRELLO_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
TRELLO_BOARD_ID = os.environ["TRELLO_BOARD_ID"]

# LLM settings
LLM_API_KEY = os.environ["LLM_API_KEY"]
LLM_URL = os.environ["LLM_URL"]
LLM_MODEL = os.environ["LLM_MODEL"]

# Google Drive settings
GOOGLE_DRIVE_FOLDER_ID = os.environ["GOOGLE_DRIVE_FOLDER_ID"]
GOOGLE_SERVICE_ACCOUNT_INFO = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

# Optional PDF output
CREATE_PDF = os.environ.get("CREATE_PDF", "false").lower() == "true"


SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]


def is_gemini() -> bool:
    return "googleapis.com" in LLM_URL


def format_trello_cards(cards: list[dict]) -> str:
    lines = []
    for card in cards:
        name = card.get("name", "Untitled")
        desc = card.get("desc", "").strip()
        list_name = card.get("list", {}).get("name", "Unknown list")
        due = card.get("due")
        members = ", ".join(m.get("fullName", "") for m in card.get("members", []))

        lines.append(f"Card: {name}")
        lines.append(f"List: {list_name}")
        if due:
            lines.append(f"Due: {due}")
        if members:
            lines.append(f"Members: {members}")
        if desc:
            lines.append(f"Description:\n{desc}")
        lines.append("")
    return "\n".join(lines)


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


def build_prompt(cards_text: str) -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
    return (
        f"Today is {today}. Summarize the following Trello board cards as a weekly progress report. "
        "Group by list/status, highlight completed work, in-progress work, and blockers. "
        "Use plain language and bullet points. Do not invent facts that are not in the cards.\n\n"
        f"{cards_text}"
    )


def gemini_payload(prompt: str) -> dict:
    return {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 4096},
    }


def openai_payload(prompt: str) -> dict:
    return {
        "model": LLM_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant writing a weekly Trello board summary. "
                    "Be concise, accurate, and use bullet points."
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

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"LLM API error {resp.status}: {json.dumps(data)}")
            if is_gemini():
                return data["candidates"][0]["content"]["parts"][0]["text"]
            return data["choices"][0]["message"]["content"]


def get_drive_service():
    creds_info = json.loads(GOOGLE_SERVICE_ACCOUNT_INFO)
    credentials = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
    )
    return build("drive", "v3", credentials=credentials)


def get_docs_service():
    creds_info = json.loads(GOOGLE_SERVICE_ACCOUNT_INFO)
    credentials = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
    )
    return build("docs", "v1", credentials=credentials)


def create_google_doc(title: str, body_text: str) -> str:
    """Create a new Google Doc with the given title and body. Returns doc URL."""
    drive_service = get_drive_service()
    docs_service = get_docs_service()

    # Create the document.
    doc = docs_service.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]

    # Move it to the target folder if one is set.
    if GOOGLE_DRIVE_FOLDER_ID:
        drive_service.files().update(
            fileId=doc_id,
            addParents=GOOGLE_DRIVE_FOLDER_ID,
            fields="id,parents",
        ).execute()

    # Insert body text.
    requests = [{"insertText": {"location": {"index": 1}, "text": body_text}}]
    docs_service.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()

    return f"https://docs.google.com/document/d/{doc_id}/edit"


def export_doc_to_pdf(doc_id: str, pdf_title: str) -> str:
    """Export a Google Doc to PDF and upload it to the same Drive folder. Returns PDF URL."""
    drive_service = get_drive_service()

    # Export as PDF.
    request = drive_service.files().export_media(fileId=doc_id, mimeType="application/pdf")
    pdf_bytes = request.execute()

    # Upload PDF to Drive.
    file_metadata = {
        "name": pdf_title,
        "parents": [GOOGLE_DRIVE_FOLDER_ID] if GOOGLE_DRIVE_FOLDER_ID else [],
        "mimeType": "application/pdf",
    }
    pdf_file = (
        drive_service.files()
        .create(body=file_metadata, media_body=None, fields="id, webViewLink")
        .execute()
    )

    # Upload media separately for PDF binary.
    from googleapiclient.http import MediaIoBaseUpload
    import io

    media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf")
    pdf_file = (
        drive_service.files()
        .update(fileId=pdf_file["id"], media_body=media, fields="id, webViewLink")
        .execute()
    )

    return pdf_file.get("webViewLink", f"https://drive.google.com/file/d/{pdf_file['id']}/view")


async def main() -> None:
    today = datetime.now().strftime("%Y-%m-%d")

    cards = await fetch_trello_cards()
    if not cards:
        print("No cards found on board.")
        return

    cards_text = format_trello_cards(cards)
    prompt = build_prompt(cards_text)
    summary = await summarize(prompt)

    doc_title = f"Trello Weekly Summary - {today}"
    doc_url = create_google_doc(doc_title, summary)
    print(f"Created Google Doc: {doc_url}")

    if CREATE_PDF:
        pdf_url = export_doc_to_pdf(
            doc_url.split("/d/")[1].split("/")[0],
            f"Trello Weekly Summary - {today}.pdf",
        )
        print(f"Created PDF: {pdf_url}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
