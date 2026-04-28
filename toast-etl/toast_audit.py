#!/usr/bin/env python3
"""Toast partner-client capabilities audit.

Authenticates against the Toast API with each provided (client_id, client_secret)
pair and reports what each token can do. Run from a workflow that has both the
Standard and Analytics client secrets in env, so we can compare side by side
without copy/paste.

For each client pair we:
  1. Auth and decode the JWT to surface scopes / clientId / aud.
  2. Hit GET /partners/v1/restaurants — confirms which restaurant guids are
     actually associated with this partner client.
  3. Try a tiny standard-API roundtrip:
       GET /labor/v1/jobs (with the first associated restaurant guid)
  4. Try the Analytics report flow on each topic. Each is async:
       POST /era/v1/<topic>/...  -> reportRequestGuid
       GET  /era/v1/<topic>/{guid} -> {status, data}
     We poll a couple of times for COMPLETED. Topics probed:
       - metrics/day        (sales aggregation)
       - labor              (labor reporting)
       - menu               (item mix + waste)
       - check              (check-level w/ server, tip, gratuity)

For each probe we print one line:
   [client=STANDARD] /era/v1/menu — POST 200 -> guid=... -> COMPLETED in 3s
or:
   [client=ANALYTICS] /era/v1/menu — POST 403 forbidden (insufficient_scope)

Output is plain text on stdout; this is meant to be read in a workflow log.

Usage:
  TOAST_STANDARD_CLIENT_ID=... TOAST_STANDARD_CLIENT_SECRET=... \\
  TOAST_ANALYTICS_CLIENT_ID=... TOAST_ANALYTICS_CLIENT_SECRET=... \\
  python3 toast_audit.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write("missing dependency: pip install requests\n")
    sys.exit(2)


TOAST_BASE = (os.environ.get("TOAST_BASE") or "https://ws-api.toasttab.com").rstrip("/")
REQUEST_TIMEOUT = 30
POLL_TIMES = 8       # how many times we re-check a report request
POLL_INTERVAL = 2.0  # seconds between polls


def _b64_pad(s: str) -> str:
    """Add the missing padding to a base64url string."""
    return s + "=" * (-len(s) % 4)


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Best-effort decode of a JWT payload. Returns {} if it isn't a JWT."""
    try:
        _, body, _ = token.split(".", 2)
        return json.loads(base64.urlsafe_b64decode(_b64_pad(body)))
    except Exception:
        return {}


def get_token(client_id: str, client_secret: str) -> str | None:
    """Authenticate as a partner machine client. Returns the access token or None."""
    url = f"{TOAST_BASE}/authentication/v1/authentication/login"
    try:
        r = requests.post(
            url,
            json={
                "clientId": client_id,
                "clientSecret": client_secret,
                "userAccessType": "TOAST_MACHINE_CLIENT",
            },
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  AUTH ERROR: {e}")
        return None
    if r.status_code != 200:
        print(f"  AUTH FAILED: {r.status_code} {r.text[:240]}")
        return None
    return (r.json().get("token") or {}).get("accessToken")


def list_restaurants(token: str) -> list[dict[str, Any]]:
    url = f"{TOAST_BASE}/partners/v1/restaurants"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        print(f"  /partners/v1/restaurants -> {r.status_code} {r.text[:200]}")
        return []
    body = r.json() or []
    return body if isinstance(body, list) else body.get("results", [])


def probe_jobs(token: str, guid: str) -> str:
    url = f"{TOAST_BASE}/labor/v1/jobs"
    headers = {"Authorization": f"Bearer {token}", "Toast-Restaurant-External-ID": guid}
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if r.status_code == 200:
        body = r.json() or []
        rows = body if isinstance(body, list) else body.get("results", [])
        return f"OK ({len(rows)} jobs returned)"
    return f"{r.status_code} {r.text[:160]}"


def _request_report(
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | str]:
    url = f"{TOAST_BASE}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.request(method, url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, r.text[:240]


def probe_era(token: str, restaurant_guid: str) -> dict[str, str]:
    """Run a tiny request on each ERA topic and report status.

    Bodies match what doc.toasttab.com/devguide/apiAnalytics* documents:
    only `startBusinessDate` + `restaurantIds` are required. The previous
    probe sent `endBusinessDate` which isn't in the spec; ERA replied 400.

    For `metrics_day` we run two probes — minimal (no groupBy) and rich
    (groupBy DINING_OPTION + REVENUE_CENTER) — so we know whether
    rejection is on the body itself or just on a specific groupBy value.
    """
    last_week = (date.today() - timedelta(days=7)).strftime("%Y%m%d")

    probes: list[tuple[str, str, dict[str, Any]]] = [
        # name, post_path, post_body
        ("metrics_day_min",
         "/era/v1/metrics/day",
         {
             "startBusinessDate": last_week,
             "restaurantIds":     [restaurant_guid],
         }),
        ("metrics_day_rich",
         "/era/v1/metrics/day",
         {
             "startBusinessDate": last_week,
             "restaurantIds":     [restaurant_guid],
             "groupBy":           ["DINING_OPTION", "REVENUE_CENTER"],
         }),
        ("labor",
         "/era/v1/labor",
         {
             "startBusinessDate": last_week,
             "restaurantIds":     [restaurant_guid],
         }),
        ("menu",
         "/era/v1/menu",
         {
             "startBusinessDate": last_week,
             "restaurantIds":     [restaurant_guid],
         }),
        # Toast docs reference /era/v1/check but our first probe got 404
        # for both clients, suggesting either the endpoint name differs or
        # it requires a different access tier. Probe once at the documented
        # path; if 404 again, leave a note in the next iteration.
        ("check",
         "/era/v1/check",
         {
             "startBusinessDate": last_week,
             "restaurantIds":     [restaurant_guid],
         }),
    ]

    results: dict[str, str] = {}
    for name, post_path, body in probes:
        post_status, post_body = _request_report("POST", post_path, token, body)
        if post_status not in (200, 201, 202):
            snippet = json.dumps(post_body)[:240] if isinstance(post_body, dict) else str(post_body)[:240]
            results[name] = f"POST {post_status} {snippet}"
            continue

        guid = (post_body or {}).get("reportRequestGuid") if isinstance(post_body, dict) else None
        if not guid:
            results[name] = f"POST {post_status} OK but no reportRequestGuid in body: {str(post_body)[:160]}"
            continue

        get_path = f"{post_path}/{guid}"
        # Poll for COMPLETED
        for i in range(POLL_TIMES):
            get_status, get_body = _request_report("GET", get_path, token)
            if get_status != 200:
                results[name] = f"POST 200 -> GET {get_status} {str(get_body)[:160]}"
                break
            status = (get_body or {}).get("status") if isinstance(get_body, dict) else None
            if status == "COMPLETED":
                # data may be array or {data: ...} depending on endpoint
                data = get_body.get("data") if isinstance(get_body, dict) else get_body
                row_count = (
                    len(data) if isinstance(data, list)
                    else (len(data.get("data") or []) if isinstance(data, dict) and isinstance(data.get("data"), list) else "n/a")
                )
                results[name] = f"OK COMPLETED in ~{(i+1)*POLL_INTERVAL:.0f}s (rows={row_count})"
                break
            if status in ("FAILED", "ERROR"):
                results[name] = f"OK POST -> {status} {str(get_body)[:160]}"
                break
            time.sleep(POLL_INTERVAL)
        else:
            results[name] = f"POST 200 -> never COMPLETED after {POLL_TIMES * POLL_INTERVAL:.0f}s; last status={status!r}"
    return results


def parse_outlets_guids() -> list[tuple[str, str]]:
    """Pull (label, restaurantGuid) pairs out of TOAST_OUTLETS.

    Reuses the same string format as toast_sync.py but only extracts the
    restaurant GUID per rc_key (skipping @rc=/@rc_not= filter suffixes).
    Returns deduped pairs preserving discovery order — useful when the
    analytics client needs explicit GUIDs to query against.
    """
    raw = (os.environ.get("TOAST_OUTLETS") or "").strip()
    if not raw:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        outlet_id, rest = chunk.split("=", 1)
        for rc in rest.split(","):
            rc = rc.strip()
            if ":" not in rc:
                continue
            rc_key, guid_with_filter = rc.split(":", 1)
            guid = guid_with_filter.split("@", 1)[0].strip()
            if guid and guid not in seen:
                seen.add(guid)
                out.append((f"{outlet_id.strip()}:{rc_key.strip()}", guid))
    return out


def audit_client(
    label: str,
    client_id: str | None,
    client_secret: str | None,
    outlet_guids: list[tuple[str, str]],
) -> None:
    print(f"\n=========== client={label} ===========")
    if not client_id or not client_secret:
        print("  SKIP — env not set")
        return

    print(f"  client_id (first 8): {client_id[:8]}...   secret length: {len(client_secret)}")
    token = get_token(client_id, client_secret)
    if not token:
        return

    # JWT introspection — Toast access tokens carry both the standard claims
    # and Toast-namespaced ones (https://toasttab.com/partner_guid, etc).
    payload = decode_jwt_payload(token)
    if payload:
        print("  jwt payload:")
        # Surface the Toast-namespaced claims first (most informative)
        toast_keys = [k for k in payload if k.startswith("https://toasttab.com/")]
        for k in toast_keys:
            print(f"    {k.split('/')[-1]}: {payload[k]}")
        # Then standard JWT claims we care about
        for k in ("client_id", "clientId", "scope", "scopes", "aud", "iss", "sub", "azp", "exp"):
            if k not in payload: continue
            v = payload[k]
            if k == "exp":
                v = datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
            print(f"    {k}: {v}")
    else:
        print("  jwt payload: (no introspectable fields)")

    # Standard client owns /partners/v1/restaurants. Analytics client doesn't —
    # for it we'll seed probes from TOAST_OUTLETS instead.
    rests = list_restaurants(token)
    if rests:
        print(f"  /partners/v1/restaurants: {len(rests)} restaurant(s) associated")
        for r in rests[:15]:
            name = r.get('restaurantName') or r.get('name') or '(unnamed)'
            gid = r.get('restaurantGuid') or r.get('guid') or '?'
            print(f"    - {name}  guid={gid[:8]}...")
        if len(rests) > 15:
            print(f"    ... and {len(rests) - 15} more")

        # Surface the delta vs TOAST_OUTLETS — helpful to find restaurants
        # the partner client can see but we're not syncing.
        if outlet_guids:
            outlet_guid_set = {g for _, g in outlet_guids}
            associated_guids = {(r.get('restaurantGuid') or r.get('guid') or '') for r in rests}
            extras = associated_guids - outlet_guid_set
            missing = outlet_guid_set - associated_guids
            if extras:
                print(f"\n  Restaurants visible to client BUT NOT in TOAST_OUTLETS ({len(extras)}):")
                for r in rests:
                    gid = r.get('restaurantGuid') or r.get('guid') or ''
                    if gid in extras:
                        print(f"    - {r.get('restaurantName') or r.get('name') or '(unnamed)'}  guid={gid}")
            if missing:
                print(f"\n  Restaurants in TOAST_OUTLETS BUT NOT visible to client ({len(missing)}):")
                for outlet, gid in outlet_guids:
                    if gid in missing:
                        print(f"    - {outlet}  guid={gid}")

    # Pick a single GUID to probe ERA with. Prefer one that's actually in
    # TOAST_OUTLETS so we know it's a real synced outlet, not a sandbox.
    probe_guid: str | None = None
    if outlet_guids:
        probe_guid = outlet_guids[0][1]
        probe_label = outlet_guids[0][0]
    elif rests:
        probe_guid = rests[0].get('restaurantGuid') or rests[0].get('guid')
        probe_label = rests[0].get('restaurantName') or '(first associated)'

    if probe_guid:
        print(f"\n  Standard /labor/v1/jobs probe ({probe_label}, guid={probe_guid[:8]}...): {probe_jobs(token, probe_guid)}")
        print(f"\n  Analytics /era/v1/* probes (guid={probe_guid[:8]}...):")
        for name, status in probe_era(token, probe_guid).items():
            print(f"    {name:14s} {status}")
    else:
        print("  no GUIDs available — cannot probe further (set TOAST_OUTLETS or check restaurants assoc.)")


def main() -> int:
    print(f"Toast capability audit — base={TOAST_BASE}  ts={datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    outlet_guids = parse_outlets_guids()
    if outlet_guids:
        print(f"TOAST_OUTLETS contains {len(outlet_guids)} unique restaurant GUID(s).")
    audit_client(
        "STANDARD",
        os.environ.get("TOAST_STANDARD_CLIENT_ID"),
        os.environ.get("TOAST_STANDARD_CLIENT_SECRET"),
        outlet_guids,
    )
    audit_client(
        "ANALYTICS",
        os.environ.get("TOAST_ANALYTICS_CLIENT_ID") or os.environ.get("TOAST_CLIENT_ID"),
        os.environ.get("TOAST_ANALYTICS_CLIENT_SECRET"),
        outlet_guids,
    )
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
