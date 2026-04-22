# Toast ETL -- Method Co F&B Performance Dashboard

Nightly pull of Toast orders for each configured outlet. The script auths
against the Toast API with a partner machine-client OAuth token, fetches
orders through `/orders/v2/ordersBulk` day-by-day, transforms them into the
shape the dashboard expects, and writes one JSON file per outlet to
`../data/<outlet>.json`. The GitHub Action commits any changes back to the
repo so the static site on GitHub Pages refreshes automatically.

## Files

| Path | Purpose |
| --- | --- |
| `toast_sync.py` | The ETL. Auth + fetch + transform + write. |
| `.github/workflows/toast-sync.yml` | Nightly cron + manual dispatch. |
| `../data/<outlet>.json` | Output. Checked in; consumed by the dashboard. |

Drop both files into `rrmethodco/method-q1-2026-dashboards`:

```
method-q1-2026-dashboards/
  Method_Co_FB_Performance_Dashboard.html
  data/                                  <- outputs land here
    lsbr.json
    _synced_at.txt
  toast-etl/
    toast_sync.py
    README.md
  .github/
    workflows/
      toast-sync.yml
```

(Note: keep `.github/workflows/toast-sync.yml` at the **repo root** `.github/`, not inside `toast-etl/.github/`. Move it up one level when you commit -- it's nested here because this workspace folder sits outside the git repo.)

## Required secrets

Add these to repo **Settings -> Secrets and variables -> Actions**:

| Secret | Value |
| --- | --- |
| `TOAST_CLIENT_ID` | Partner client id from the Toast developer portal. |
| `TOAST_CLIENT_SECRET` | Partner client secret. |
| `TOAST_OUTLETS` | Outlet->RC->guid mapping. See format below. |
| `TOAST_BASE` | *Optional.* Override for sandbox. Default prod: `https://ws-api.toasttab.com`. |

### `TOAST_OUTLETS` format

One outlet per semicolon-separated chunk. Inside each chunk, `outlet_id=rc_key:guid[@filter][,rc_key:guid[@filter]]`:

```
hiroki_phl=main:45d266c6-7dd1-43cb-9b09-2c157f277a3c;
anthology=main:805eee0d-3a41-42a1-bd7a-f400363e9fd9
```

`outlet_id` and `rc_key` must match the ids the dashboard already uses (e.g. `lsbr`, `le_supreme`, `bar_rotunda`).

**Optional `@filter` suffix** — lets one restaurantGuid feed multiple rc_keys by Toast Revenue Center name:

| Filter | Behavior |
| --- | --- |
| `@rc=Name1\|Name2` | Keep only orders whose RC name is in this list |
| `@rc_not=Name1\|Name2` | Keep everything except these RCs (plus orders with no RC assigned — catch-all bucket) |

**LSBR split** (one Toast instance, two concepts — 1 outlet, 2 rc_keys):

```
lsbr=bar_rotunda:99d1583c-6e31-43dc-89f8-8d2e19ac147b@rc=Bar Rotunda,le_supreme:99d1583c-6e31-43dc-89f8-8d2e19ac147b@rc_not=Bar Rotunda
```

The `include` + `exclude` of the same name form a disjoint partition — every order lands in exactly one rc_key. Orders with no RC assigned fall into the `@rc_not=` bucket. Toast RC names for LSBR (as of 2026-04-22): Bar Rotunda, Le Supreme Bar, Main Dining Room, Bar Dining, PDR, Patio.

**HIROKI-SAN Detroit split** (3-way: one Toast instance, three concepts — 1 outlet, 3 rc_keys):

```
hiroki_det=hiroki_san:1480f7c0-7b54-4c74-bec9-862f8e0efc0e@rc_not=Sakazuki|Aladdin Sane,sakazuki:1480f7c0-7b54-4c74-bec9-862f8e0efc0e@rc=Sakazuki,aladdin_sane:1480f7c0-7b54-4c74-bec9-862f8e0efc0e@rc=Aladdin Sane
```

`hiroki_san` uses `@rc_not=` to become the catch-all for no-RC orders and any future new RCs, per Ross's 2026-04-22 confirmation. `sakazuki` and `aladdin_sane` use strict `@rc=` include. Toast RC names for hiroki_det: HIROKI-SAN, Sakazuki, Aladdin Sane.

**Multi-property shared-GUID** (one Toast instance serving two distinct properties — 2 outlets, same GUID):

```
little_wing=main:a8a82047-9940-4be2-a090-d3da71b51fe4@rc_not=Vessel Upstairs Bar|Vessel - Upstairs Patio;
vessel=main:a8a82047-9940-4be2-a090-d3da71b51fe4@rc=Vessel Upstairs Bar|Vessel - Upstairs Patio
```

Same `@rc=` / `@rc_not=` mechanism as LSBR, but at the outlet level: two outlet entries reference the same GUID with complementary filters, producing two independent outlet JSON files. Use when the Toast instance hosts truly separate properties (not two concepts of one property). Toast RC names for Little Wing (confirmed 2026-04-22 by Ross): Little Wing, Online Ordering (→ little_wing catch-all); Vessel Upstairs Bar, Vessel - Upstairs Patio (→ vessel). No-RC orders land in `little_wing`.

**Multi-GUID per outlet** (different concepts, different GUIDs, grouped under one property):

```
quoin=quoin_restaurant:2d1d8888-91d2-41a4-8167-3ba7b0c10c90,quoin_rooftop:01ed27a0-c29a-4780-8ee5-96a860f7f0f7,simmer_down:fdef7a8f-2a5b-44da-baa1-5c5b9e9f5d7d
```

Each rc_key gets its own GUID (no RC filter needed since each is a distinct Toast instance). `@filter` and multi-GUID can be combined freely.

**Multi-GUID rollup** (merge multiple legacy GUIDs into a single rc_key):

```
legacy_rollup=main:GUID-A,main:GUID-B,main:GUID-C
```

The ETL fetches each GUID once, then `transform_orders` sees the combined order list for that rc_key.

## Output shape

Per outlet, the script writes:

```json
{
  "outlet_id": "lsbr",
  "generated_at": "2026-04-22T07:15:03+00:00",
  "source": "toast_api_v2",
  "order_details": {
    "le_supreme":  { "daily": [...], "monthly": [...], "hour_dow": [...], "servers": [...], "tables": [...], "totals": {"tip_bins": [11 ints]} },
    "bar_rotunda": { ... }
  }
}
```

This maps 1:1 to `DATA.outlets[outletId].order_details[rcKey]` in the HTML. The dashboard already reads these fields at lines 463, 1177, 1216, 1258, 1291, 1326, 1344.

## Dashboard wiring (next step)

Right now the HTML carries its DATA blob inline. To consume these JSON
files we add a tiny loader that merges the fetched payload into `DATA`
at boot -- I'll send that as a small diff once this ETL is running clean.
Short version:

```js
// near the top of the script block, after DATA is defined:
(async () => {
  const outlets = Object.keys(DATA.outlets);
  await Promise.all(outlets.map(async id => {
    try {
      const r = await fetch(`data/${id}.json`, {cache: 'no-store'});
      if (!r.ok) return;
      const payload = await r.json();
      DATA.outlets[id].order_details = payload.order_details;
    } catch (_) { /* keep inline fallback */ }
  }));
  boot();  // existing init fn
})();
```

## Local dry-run (no credentials)

```
cd toast-etl
python3 toast_sync.py --dry-run --outdir ../data
```

Produces a realistic `../data/lsbr.json` fixture so you can verify the
dashboard loads the payload before wiring real secrets.

## Local live run

```
export TOAST_CLIENT_ID=...
export TOAST_CLIENT_SECRET=...
export TOAST_OUTLETS='lsbr=le_supreme:GUID-A,bar_rotunda:GUID-B'
pip install requests
python3 toast_sync.py --outlet lsbr --days 60 --outdir ../data
```

## Close-time semantics

Per Ross: the "close time" used for hour-of-day / Avg Ticket Time is the
**payment time** on the check. The script reads `check.paidDate` first,
then falls back to `check.closedDate`, then `order.closedDate`. Tweak
`transform_orders()` if you want strict paid-only.

## Timezone caveat

The current transform folds orders into hour/DOW using UTC. For the
Detroit vs. Philadelphia outlets this will skew the heatmap by 4-5
hours. Fix before production: add a per-outlet IANA timezone to
`TOAST_OUTLETS` (e.g. `lsbr=le_supreme:guid:America/Detroit,...`) and
convert in `_as_local_date()`. I'll ship this alongside the dashboard
loader diff.

## Rate limits

Toast enforces per-client rate limits. Defaults in the script are
conservative:

- `PAGE_SIZE = 100` (max)
- `SLEEP_BETWEEN_PAGES = 0.25s`
- Day-by-day window fetch (keeps each query < 1h of orders)

A full 400-day pull for 2 RCs takes ~3-5 min in practice.

## Troubleshooting

- **401 from `/authentication/v1/authentication/login`** -- client id/secret typo, or the client isn't approved for the production environment (check the Toast developer portal).
- **403 on ordersBulk** -- the `Toast-Restaurant-External-ID` guid isn't associated with the partner client. Contact Toast support to attach locations.
- **Empty `daily` array** -- check that `paidDate` is present on checks. Test orders (sandbox) often skip payment.
