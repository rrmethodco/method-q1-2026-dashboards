# MarginEdge Integration Memo — Purchase / COGS Data

_Prepared 2026-04-28 for Ross Richardson (EVP F&A, Method Co.)_

## 1. Is there a documented public API?

**Yes.** MarginEdge ships a **Public REST API**, included in every subscription at no extra cost. Two canonical sources:

- Help-center overview: `help.marginedge.com/hc/en-us/articles/28081506932499-MarginEdge-Public-API`
- Developer portal (full reference): `developer.marginedge.com` (JS SPA — must be opened in a browser; behind Cloudflare so curl/WebFetch returns 403)

It is **read-only / one-way** (MarginEdge → external). It cannot create or update records.

## 2. What's available for nightly extraction?

Per MarginEdge's own description, the Public API exposes:

- **Invoices** (with line items)
- **Products**
- **Vendors**
- **Vendor items**
- **Categories**

That is sufficient to compute **vendor spend, product-level cost, and weekly COGS from invoices**, and — joined to Method's existing Toast net-sales feed — **weekly COGS % of net sales by outlet**.

**Explicitly NOT available via Public API** (confirmed in MarginEdge's own help article):

- **Inventory data** (so true ideal-vs-actual / variance is not API-accessible)
- **Daily sales entries** (the rolled-up DSS that ME exports to accounting)
- **Recipes / theoretical food cost / food-cost %** — not listed as a supported entity

For ideal-vs-actual and food-cost % specifically, the API is **not the path**; those live only in the ME UI / CSV exports.

## 3. Per-outlet scoping

**Per-restaurant, not org-wide.** Authorization is granted by each restaurant individually ("only those authorized by a restaurant to retrieve data for that restaurant can access the data"). For Method's ~11 outlets that means **one auth grant per outlet**, then per-outlet calls — which actually fits the existing per-outlet JSON architecture cleanly.

## 4. Auth model

Not publicly documented outside the dev portal SPA, which couldn't be rendered headlessly during research. What's verified: a restaurant-admin must explicitly authorize the integration, and credentials are scoped to that restaurant. Industry-standard pattern (and consistent with the dev portal's existence) is **API key or OAuth client-credentials per location**. **Confirm the exact flow from the dev portal directly when logged in, or via ME support** — do not assume.

## 5. Rate limits / volume

**Not publicly documented.** No published RPS, daily quota, or pagination details surfaced in any indexed help article or third-party source. For a nightly job pulling ~11 outlets' invoices, this is almost certainly a non-issue, but worth confirming with ME support before building.

## 6. Toast integration patterns

MarginEdge already pulls Toast sales + labor in via Toast's API daily (Toast charges a $50/loc/mo "Restaurant Management Suite" fee for this). That's an **inbound feed into ME**, not a pattern Method can reuse.

For the dashboard, keep them as **two parallel feeds** — Toast direct (already wired) for sales/labor, MarginEdge Public API for purchases — and join on `outlet × week` in the dashboard layer. Don't try to route Toast through ME.

## 7. Practical fallbacks if API blocks

In priority order:

1. **Native CSV export** — Controllable P&L and most reports support per-location CSV/PDF export in the UI; can be scripted via Playwright/headless browser if API gaps (recipes, ideal-vs-actual) are needed.
2. **EDI / SFTP** — ME confirms it operates **nightly SFTP export jobs** for accounting integrations (e.g., M3). May be available for custom destinations — ask ME support.
3. **Direct ask to MarginEdge support / CSM** — fastest path to confirm auth model, rate limits, and whether Method's enterprise tier unlocks anything beyond the standard Public API. Method has 11 paid seats; that's leverage.
4. **Zapier/Workato** — no first-party listing surfaced; not recommended as a primary path.

## Recommended next step

Email Method's MarginEdge CSM and ask for:

1. The per-location auth flow + sample credentials for one ROOST outlet as a pilot.
2. Documented rate limits.
3. Whether nightly SFTP delivery of invoice line items is available for non-accounting endpoints.

Pilot with one outlet against `developer.marginedge.com` before fanning out to all 11. Once authenticated, add a `marginedge_sync.py` ETL alongside `toast_sync.py` and `resy_scraper.py`, writing `purchases` / `cogs_weekly` blocks into `data/<outlet>.json`. The dashboard can then render a Cost of Goods section with weekly COGS %, top-vendor spend, and category trend lines — and the Weekly Snapshot's prescriptive engine can flag COGS-% deviations alongside labor%.

## Sources

- [MarginEdge Public API — help center](https://help.marginedge.com/hc/en-us/articles/28081506932499-MarginEdge-Public-API)
- [MarginEdge Developer Portal](https://developer.marginedge.com/)
- [Connecting [me] to Toast](https://help.marginedge.com/hc/en-us/articles/11316106332179-Connecting-to-Toast-via-API)
- [Customizable Accounting Integrations (SFTP/CSV)](https://help.marginedge.com/hc/en-us/articles/16317217177107-Customizable-Accounting-Integrations)
- [Controllable P&L — multi-unit CSV/PDF export](https://help.marginedge.com/hc/en-us/articles/360057326354-Controllable-P-L)
- [Toast + MarginEdge integration page](https://www.marginedge.com/integration/toast)
