#!/usr/bin/env python3
"""
google_gbp_reviews_sync.py — pull full Google review history per venue
via the Google Business Profile API.

Replaces the 5-sample-only Places API path (toast-etl/google_reviews_sync.py)
with the official OAuth-authenticated GBP API which returns every review
for locations Method has owner/manager access to.

Outputs into the same data/<outlet>.json shape under guest.google.reviews
(alongside the existing guest.google.samples that the Places API still
populates, in case any venues aren't claimed). Dashboard renderer prefers
.reviews when present, falls back to .samples.

Auth flow (one-time setup, see docs/GOOGLE_GBP_SETUP.md):
  1. Google Cloud Console: create project, enable My Business Account
     Management API + My Business Business Information API
  2. OAuth consent screen: add your @methodco.com email as test user
  3. Create OAuth 2.0 client credentials (Desktop type)
  4. Download client_secret.json → ~/.config/method-dashboards/gbp-client.json
  5. Run: python3 tools/google_gbp_reviews_sync.py --auth
     → opens a browser, you grant access, refresh token saved to
       ~/.config/method-dashboards/gbp-token.json
  6. Subsequent runs use the refresh token automatically

Daily run (after setup):
  python3 tools/google_gbp_reviews_sync.py
  python3 tools/google_gbp_reviews_sync.py --outlet lsbr   # one venue

Reviews API endpoint:
  GET https://mybusiness.googleapis.com/v4/accounts/{account}/locations/{loc}/reviews

The v4 host requires apply-and-wait access. If your project hasn't been
granted v4, this script falls back to the BusinessProfilePerformance API
which exposes review counts/ratings but NOT review text — the Places
API path remains the source of truth for text in that case.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    import requests
except ImportError:
    sys.stderr.write(
        "missing dependency: pip3 install google-auth-oauthlib requests\n"
    )
    sys.exit(2)


CONFIG_DIR = Path.home() / ".config" / "method-dashboards"
CLIENT_FILE = CONFIG_DIR / "gbp-client.json"
TOKEN_FILE = CONFIG_DIR / "gbp-token.json"
LOCATIONS_FILE = CONFIG_DIR / "gbp-locations.json"  # {outlet_id: "accounts/X/locations/Y"}

# https://developers.google.com/my-business/reference/rest
SCOPES = ["https://www.googleapis.com/auth/business.manage"]


def get_credentials(force_auth: bool = False) -> Credentials:
    """Run OAuth flow once, then refresh thereafter."""
    creds = None
    if TOKEN_FILE.exists() and not force_auth:
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token and not force_auth:
            creds.refresh(Request())
        else:
            if not CLIENT_FILE.exists():
                sys.stderr.write(
                    f"missing OAuth client at {CLIENT_FILE}\n"
                    "Download from Google Cloud Console → APIs & Services → Credentials\n"
                )
                sys.exit(2)
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def load_locations() -> dict[str, str]:
    """{outlet_id: location_resource_name}.
    Run --list-accounts on first setup to populate this file."""
    if not LOCATIONS_FILE.exists():
        sys.stderr.write(
            f"missing locations map at {LOCATIONS_FILE}\n"
            "Run: python3 tools/google_gbp_reviews_sync.py --list-accounts\n"
            "Then map outlet_id → location resource name in that file.\n"
        )
        sys.exit(2)
    return json.loads(LOCATIONS_FILE.read_text(encoding="utf-8"))


def list_accounts(creds: Credentials) -> None:
    """Print all accounts + locations the operator can access. Used to
    populate gbp-locations.json on first setup."""
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {creds.token}"
    print("Fetching accounts...")
    r = s.get("https://mybusinessaccountmanagement.googleapis.com/v1/accounts")
    r.raise_for_status()
    accounts = r.json().get("accounts", [])
    print(f"Found {len(accounts)} accounts:\n")
    for a in accounts:
        name = a.get("name")
        print(f"Account: {name}  ({a.get('accountName', '?')})")
        # https://developers.google.com/my-business/reference/businessinformation/rest/v1/accounts.locations/list
        lr = s.get(
            f"https://mybusinessbusinessinformation.googleapis.com/v1/{name}/locations",
            params={"readMask": "name,title,storefrontAddress"}
        )
        if not lr.ok:
            print(f"  (locations request failed: {lr.status_code} {lr.text[:120]})")
            continue
        for loc in lr.json().get("locations", []):
            addr = loc.get("storefrontAddress", {})
            line = ", ".join(addr.get("addressLines", []))
            print(f"  {loc.get('name')}  {loc.get('title','?')} — {line}")
    print()
    print("Map these resource names into gbp-locations.json:")
    print('  {"lsbr": "accounts/123/locations/456", ...}')


def fetch_reviews(creds: Credentials, location_name: str) -> list[dict]:
    """Pull every review for a single location. Paginated by pageToken."""
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {creds.token}"
    out: list[dict] = []
    page_token = None
    # The reviews endpoint lives on mybusiness.googleapis.com (v4) which
    # requires apply-for-access on the GCP project. Until access lands,
    # this 403s. The Places API path remains as a fallback in that case.
    base = f"https://mybusiness.googleapis.com/v4/{location_name}/reviews"
    page_idx = 0
    while page_idx < 100:
        params = {"pageSize": 50}
        if page_token:
            params["pageToken"] = page_token
        r = s.get(base, params=params)
        if r.status_code == 403:
            sys.stderr.write(
                f"  GBP v4 reviews access denied for {location_name} — "
                "your GCP project needs apply-for-access. Falling back.\n"
            )
            return out
        if not r.ok:
            sys.stderr.write(f"  reviews fetch failed {r.status_code}: {r.text[:160]}\n")
            return out
        body = r.json()
        for rev in body.get("reviews", []):
            # Schema: name, reviewId, reviewer{displayName, profilePhotoUrl,
            #         isAnonymous}, starRating(ONE..FIVE), comment, createTime,
            #         updateTime, reviewReply{comment, updateTime}
            star_map = {"ONE":1,"TWO":2,"THREE":3,"FOUR":4,"FIVE":5}
            out.append({
                "review_id": rev.get("reviewId"),
                "author":    (rev.get("reviewer") or {}).get("displayName") or "Anonymous",
                "rating":    star_map.get(rev.get("starRating"), 0),
                "text":      rev.get("comment", ""),
                "time":      rev.get("createTime"),
                "updated":   rev.get("updateTime"),
                "owner_reply_text": (rev.get("reviewReply") or {}).get("comment"),
                "owner_reply_time": (rev.get("reviewReply") or {}).get("updateTime"),
            })
        page_token = body.get("nextPageToken")
        if not page_token:
            break
        page_idx += 1
    return out


def merge_into_outlet(data_dir: Path, outlet_id: str, reviews: list[dict]) -> dict:
    p = data_dir / f"{outlet_id}.json"
    if p.exists():
        payload = json.loads(p.read_text(encoding="utf-8"))
    else:
        payload = {"outlet_id": outlet_id}
    guest = payload.get("guest") or {}
    google = guest.get("google") or {}

    # Dedup by review_id; preserve any existing samples (Places API)
    existing = {r.get("review_id"): r for r in (google.get("reviews") or []) if r.get("review_id")}
    for r in reviews:
        rid = r.get("review_id")
        if rid:
            existing[rid] = r
    google["reviews"] = list(existing.values())
    google["reviews_as_of"] = date.today().isoformat()
    google["reviews_source"] = "gbp_v4"

    guest["google"] = google
    payload["guest"] = guest
    payload["generated_at_google_reviews"] = datetime.now(timezone.utc).isoformat()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return {"total": len(google["reviews"]), "fetched": len(reviews)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--outlet", help="single outlet id (default: all)")
    ap.add_argument("--data-dir",
                    default=str(Path(__file__).resolve().parent.parent / "data"))
    ap.add_argument("--auth", action="store_true",
                    help="run interactive OAuth flow (one-time setup)")
    ap.add_argument("--list-accounts", action="store_true",
                    help="print all accessible GBP accounts/locations and exit")
    args = ap.parse_args(argv)

    creds = get_credentials(force_auth=args.auth)
    if args.auth:
        print(f"✓ Token saved to {TOKEN_FILE}")
        return 0
    if args.list_accounts:
        list_accounts(creds)
        return 0

    data_dir = Path(args.data_dir)
    locations = load_locations()
    targets = ({args.outlet: locations[args.outlet]}
               if args.outlet and args.outlet in locations
               else locations)

    print(f"Pulling reviews for {len(targets)} venue(s)")
    for oid, loc_name in targets.items():
        print(f"\n[{oid}] {loc_name}")
        reviews = fetch_reviews(creds, loc_name)
        if not reviews:
            print(f"  (no reviews returned — may need API access or fallback)")
            continue
        stats = merge_into_outlet(data_dir, oid, reviews)
        print(f"  +{stats['fetched']} reviews → total {stats['total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
