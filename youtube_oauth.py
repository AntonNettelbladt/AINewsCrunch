#!/usr/bin/env python3
"""
YouTube OAuth Helper Script

This script helps you generate a YouTube refresh token for the TechNewsDaily bot.
The refresh token can be used to upload videos without requiring user interaction each time.

Prerequisites:
1. Create a Google Cloud project at https://console.cloud.google.com/
2. Enable YouTube Data API v3
3. Create OAuth 2.0 credentials (Desktop application type)
4. Download the credentials JSON file

Usage:
1. Place your OAuth credentials JSON file in the project root (or specify path)
2. Run: python youtube_oauth.py
3. Follow the browser prompts to authorize the application
4. Copy the refresh token and add it to GitHub Secrets as YT_REFRESH_TOKEN

The script will also save the credentials to credentials.json (which is gitignored).
"""

import json
import os
import sys
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
except ImportError:
    print("Error: Required packages not installed.")
    print("Please install: pip install google-auth-oauthlib google-auth-httplib2")
    sys.exit(1)


# YouTube API scopes required for uploading videos
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def get_credentials_file() -> Path:
    """Get the path to the OAuth credentials file."""
    # Check for common credential file names
    possible_names = [
        "client_secret.json",
        "credentials.json",
        "oauth_credentials.json",
        "client_id.json",
    ]
    
    for name in possible_names:
        path = Path(name)
        if path.exists():
            return path
    
    # If not found, ask user
    print("\nOAuth credentials file not found in current directory.")
    print("Please provide the path to your OAuth credentials JSON file.")
    print("(This is the file you downloaded from Google Cloud Console)")
    file_path = input("Path to credentials file: ").strip()
    
    if not file_path:
        print("Error: No file path provided")
        sys.exit(1)
    
    path = Path(file_path)
    if not path.exists():
        print(f"Error: File not found: {path}")
        sys.exit(1)
    
    return path


def main():
    """Main function to generate YouTube refresh token."""
    print("=" * 60)
    print("YouTube OAuth Helper - Generate Refresh Token")
    print("=" * 60)
    print()
    
    # Get credentials file
    creds_file = get_credentials_file()
    print(f"Using credentials file: {creds_file}")
    print()
    
    # Load client secrets
    try:
        with open(creds_file, "r") as f:
            client_config = json.load(f)
    except Exception as exc:
        print(f"Error reading credentials file: {exc}")
        sys.exit(1)
    
    # Extract client ID and secret
    if "installed" in client_config:
        client_id = client_config["installed"]["client_id"]
        client_secret = client_config["installed"]["client_secret"]
    elif "web" in client_config:
        client_id = client_config["web"]["client_id"]
        client_secret = client_config["web"]["client_secret"]
    else:
        print("Error: Invalid credentials file format")
        print("Expected 'installed' or 'web' key in JSON")
        sys.exit(1)
    
    print("Starting OAuth flow...")
    print("A browser window will open for you to authorize the application.")
    print()
    
    # Create OAuth flow
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    
    # Run the flow to get credentials
    try:
        creds = flow.run_local_server(port=0, open_browser=True)
    except Exception as exc:
        print(f"Error during OAuth flow: {exc}")
        sys.exit(1)
    
    # Extract refresh token
    if not creds.refresh_token:
        print("Error: No refresh token received")
        print("Make sure you completed the authorization process")
        sys.exit(1)
    
    refresh_token = creds.refresh_token
    
    print()
    print("=" * 60)
    print("SUCCESS! Refresh token generated")
    print("=" * 60)
    print()
    print("Add these values to your GitHub Secrets:")
    print()
    print(f"YT_CLIENT_ID={client_id}")
    print(f"YT_CLIENT_SECRET={client_secret}")
    print(f"YT_REFRESH_TOKEN={refresh_token}")
    print()
    print("=" * 60)
    print()
    
    # Save credentials for future use (optional)
    save_creds = input("Save credentials to credentials.json? (y/n): ").strip().lower()
    if save_creds == "y":
        creds_dict = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": SCOPES,
        }
        output_file = Path("credentials.json")
        with open(output_file, "w") as f:
            json.dump(creds_dict, f, indent=2)
        print(f"Credentials saved to {output_file}")
        print("(This file is gitignored and should not be committed)")
    
    print()
    print("Done! You can now use these credentials in your bot.")


if __name__ == "__main__":
    main()

