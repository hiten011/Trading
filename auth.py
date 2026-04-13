"""
Kite API Authentication
-----------------------
Step 1: Run this script to get the login URL.
Step 2: Open the URL in your browser, log in to Zerodha.
Step 3: After login, you'll be redirected to a URL like:
        http://127.0.0.1/?action=login&type=login&status=success&request_token=XXXXXXXX
Step 4: Copy the request_token from that URL and paste it here when prompted.
The access_token will be saved to access_token.txt for use by trade.py.
"""

import os
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")


def get_login_url():
    kite = KiteConnect(api_key=API_KEY)
    return kite.login_url()


def generate_access_token(request_token: str) -> str:
    kite = KiteConnect(api_key=API_KEY)
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = data["access_token"]
    # Save token for reuse during the day
    with open("access_token.txt", "w") as f:
        f.write(access_token)
    print(f"Access token saved to access_token.txt")
    return access_token


if __name__ == "__main__":
    print("=" * 60)
    print("KITE API LOGIN")
    print("=" * 60)
    print("\n1. Open this URL in your browser and log in to Zerodha:\n")
    print(f"   {get_login_url()}\n")
    print("2. After login, you'll be redirected to a URL.")
    print("   Copy the 'request_token' value from that URL.\n")

    request_token = input("Paste your request_token here: ").strip()
    if not request_token:
        print("No token provided. Exiting.")
        exit(1)

    token = generate_access_token(request_token)
    print(f"\nLogin successful! You can now run trade.py to place orders.")
