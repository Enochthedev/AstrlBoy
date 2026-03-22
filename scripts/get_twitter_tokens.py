"""
One-time script to get X/Twitter access tokens for @astrlboy__ via 3-legged OAuth.

Run this once:
  1. Enter your app's Consumer Key and Secret when prompted
  2. Open the URL in a browser where @astrlboy__ is logged in
  3. Authorize the app
  4. Enter the PIN shown
  5. Copy the access token and secret into your .env

You only need to do this once unless the tokens are revoked.

Usage:
  python scripts/get_twitter_tokens.py
"""

import tweepy


def main() -> None:
    api_key = input("Enter TWITTER_API_KEY (Consumer Key): ").strip()
    api_secret = input("Enter TWITTER_API_SECRET (Consumer Secret): ").strip()

    oauth1_handler = tweepy.OAuth1UserHandler(
        api_key,
        api_secret,
        callback="oob",
    )

    auth_url = oauth1_handler.get_authorization_url()
    print(f"\n1. Log into @astrlboy__ in your browser")
    print(f"2. Visit this URL:\n\n   {auth_url}\n")
    print(f"3. Authorize the app and copy the PIN\n")

    pin = input("Enter PIN: ").strip()

    access_token, access_token_secret = oauth1_handler.get_access_token(pin)

    print(f"\n--- Add these to your .env ---\n")
    print(f"TWITTER_ACCESS_TOKEN={access_token}")
    print(f"TWITTER_ACCESS_SECRET={access_token_secret}")


if __name__ == "__main__":
    main()
