"""Helper script to get a Google OAuth refresh token.

Run this once on your local machine. It will open a browser for you to authorize
access to your Google account, then print a refresh token.
Save that refresh token in your .env file as GOOGLE_REFRESH_TOKEN.
"""

import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv(".env")

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]


def main():
    client_config = {
        "installed": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0)

    print("\n" + "=" * 60)
    print("GOOGLE_REFRESH_TOKEN=")
    print(creds.refresh_token)
    print("=" * 60)
    print("\nCopy the refresh token above into your .env file.")


if __name__ == "__main__":
    main()
