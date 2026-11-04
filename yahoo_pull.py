#!/usr/bin/env python3
"""
yahoo_pull.py - Pull Yahoo Fantasy Football league data into Postgres.

Stores each API response as raw JSON (jsonb) in the table `yahoo_raw`
in the `fantasy` database. Query/clean it later with SQL.

First run: prints a Yahoo login link and asks you to paste back a code.
After that it uses the saved token.json and refreshes it automatically.

Needs in .env (same folder):
  YAHOO_CLIENT_ID=...
  YAHOO_CLIENT_SECRET=...
  PG_PASSWORD=...
  YAHOO_LEAGUE_ID=507376   (optional, defaults below)
"""
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import psycopg
import requests
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

CLIENT_ID = os.environ["YAHOO_CLIENT_ID"]
CLIENT_SECRET = os.environ["YAHOO_CLIENT_SECRET"]
PG_PASSWORD = os.environ["PG_PASSWORD"]
LEAGUE_ID = os.environ.get("YAHOO_LEAGUE_ID", "507376")

REDIRECT_URI = "https://localhost:8080"   # must match the Yahoo app exactly
TOKEN_FILE = BASE / "token.json"
AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API = "https://fantasysports.yahooapis.com/fantasy/v2"


# ---------- OAuth ----------

def save_token(tok):
    """Write token to disk (owner-only permissions) with an expiry timestamp."""
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    TOKEN_FILE.write_text(json.dumps(tok))
    TOKEN_FILE.chmod(0o600)
    return tok


def token_request(data):
    r = requests.post(TOKEN_URL, data=data,
                      auth=(CLIENT_ID, CLIENT_SECRET), timeout=30)
    if r.status_code != 200:
        sys.exit(f"Token request failed ({r.status_code}): {r.text}")
    return r.json()


def first_login():
    params = urlencode({"client_id": CLIENT_ID,
                        "redirect_uri": REDIRECT_URI,
                        "response_type": "code"})
    print("\n1) Open this link in any browser, log in, and click Agree:\n")
    print(f"   {AUTH_URL}?{params}\n")
    print("2) The browser will land on a page that FAILS to load. That's expected.")
    print("   Copy the value after 'code=' from the address bar.\n")
    code = input("Paste code here: ").strip()
    # In case the whole URL got pasted, pull out just the code
    if "code=" in code:
        code = code.split("code=", 1)[1].split("&", 1)[0]
    return save_token(token_request({"grant_type": "authorization_code",
                                     "code": code,
                                     "redirect_uri": REDIRECT_URI}))


def get_token():
    if not TOKEN_FILE.exists():
        return first_login()
    tok = json.loads(TOKEN_FILE.read_text())
    if time.time() >= tok.get("expires_at", 0):
        new = token_request({"grant_type": "refresh_token",
                             "refresh_token": tok["refresh_token"],
                             "redirect_uri": REDIRECT_URI})
        new.setdefault("refresh_token", tok["refresh_token"])
        tok = save_token(new)
    return tok


# ---------- API ----------

def make_getter(tok):
    def get(path):
        r = requests.get(f"{API}/{path}",
                         params={"format": "json"},
                         headers={"Authorization": f"Bearer {tok['access_token']}"},
                         timeout=30)
        if r.status_code != 200:
            sys.exit(f"API call failed for {path} ({r.status_code}): {r.text[:500]}")
        return r.json()
    return get


def main():
    get = make_getter(get_token())

    # Current NFL season's game key (changes every year), then the league key
    game_key = get("game/nfl")["fantasy_content"]["game"][0]["game_key"]
    lk = f"{game_key}.l.{LEAGUE_ID}"

    meta = get(f"league/{lk}/metadata")
    info = meta["fantasy_content"]["league"][0]
    num_teams = int(info["num_teams"])
    week = int(info["current_week"])
    print(f"League: {info.get('name')}  |  key {lk}  |  {num_teams} teams  |  week {week}")

    pulls = {
        "metadata": meta,
        "settings": get(f"league/{lk}/settings"),
        "standings": get(f"league/{lk}/standings"),
        "transactions": get(f"league/{lk}/transactions"),
        "scoreboard": get(f"league/{lk}/scoreboard;week={week}"),
    }
    for t in range(1, num_teams + 1):
        pulls[f"roster_team_{t}"] = get(f"team/{lk}.t.{t}/roster;week={week}")

    with psycopg.connect(host="127.0.0.1", port=5432, dbname="fantasy",
                         user="postgres", password=PG_PASSWORD) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS yahoo_raw (
                id          bigserial PRIMARY KEY,
                fetched_at  timestamptz NOT NULL DEFAULT now(),
                league_key  text NOT NULL,
                week        int,
                endpoint    text NOT NULL,
                payload     jsonb NOT NULL
            )""")
        for name, data in pulls.items():
            conn.execute(
                "INSERT INTO yahoo_raw (league_key, week, endpoint, payload) "
                "VALUES (%s, %s, %s, %s)",
                (lk, week, name, Jsonb(data)))
        # leaving the 'with' block commits the transaction

    print(f"Saved {len(pulls)} responses to fantasy.yahoo_raw")


if __name__ == "__main__":
    main()
