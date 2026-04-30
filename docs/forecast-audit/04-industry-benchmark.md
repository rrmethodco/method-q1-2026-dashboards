# Industry Benchmark — Where Method Stacks Up

## TL;DR

Method's forecast (helixo-2's `ai_suggested_revenue` / `ai_suggested_covers`) is roughly a 2018-vintage product strapped to a 2026 industry. State-of-the-art operators (Crunchtime, MarginEdge, R365, Toast IQ, 5-Out) all wire weather, holidays/events, reservations, and historical POS into transparent, retunable pipelines and report MAPE in the 3-7% band on daily sales. Method consumes a single opaque output, has no integrated weather/event/forward-book feed, doesn't track forecast accuracy, and cannot retrain or audit. Best estimate: Method is currently in the 12-20% MAPE range that a "well-maintained spreadsheet" produces — and we have no evidence it's better, because nobody has measured it. The fix isn't ripping out helixo-2 tomorrow; it's standing up an accuracy ledger and a Prophet/AutoARIMA shadow model so we know what we're paying for.

## Competitive landscape

| Tool | Inputs | Accuracy reported | Strength | Weakness |
|---|---|---|---|---|
| **5-Out** | POS history, reservations, labor schedule, weather, traffic, local events, holidays; 644 statistical combinations | Up to **98%** ("predicts 35 days out") | Multi-source, restaurant-purpose-built, hour-level grain | Vendor-claimed accuracy, no published MAPE |
| **Crunchtime AI Forecasting** | 60-180 days POS, weather (rain/snow/sun), holidays, promos, dine-in vs take-out, transactions | **98-99%** customer-reported; 80-99% at Jersey Mike's; +27% accuracy lift in pilots | Weather natively wired; explicit demand drivers; API for downstream sync | Enterprise-tier pricing |
| **Restaurant365 Smart Ops** | POS history + DOW/seasonality + user business rules; 15/30-min, hourly, daily, weekly grain | Not publicly published | Single ERP/forecast/labor stack; corporate roll-up across locations | Inputs lighter than peers — primarily historical POS, weaker on external regressors |
| **MarginEdge Forecasting** (launched Aug 2025) | Up to 3 yrs POS history, ordering, labor, seasonal factors; daily refresh; 90-day horizon | **Avg within 4% of actual** (≈4% MAPE) | Method already has MarginEdge across 11 outlets — zero new vendor surface | Newer product, less battle-tested than Crunchtime/5-Out |
| **Toast IQ / Toast Forecasting** | POS, weather, local events, real-time and historical Toast platform data across ~148K locations | Not published | Method already has Toast at every F&B outlet; conversational AI assistant on top | Bundled with POS — Toast won't share its model, similar opacity risk to helixo-2 |
| **Olo OrderReady AI** | Order history, channel mix | +20% lift in lead-time accuracy at P.F. Chang's | Order-channel specific | Not a sales/cover forecaster |
| **IDeaS G3 / Duetto** (hotel) | Booking pace, market demand, comp set, channel, segment, events | "AI rated 4.5/5 importance"; 86% hoteliers depend on AI forecasting | Total revenue optimization incl. F&B and meetings; ML open pricing | Hotel-rooms-first; F&B usually a side feed |
| **helixo-2 (Method, today)** | Whatever helixo-2 ingests internally; surfaces only `ai_suggested_revenue`, `ai_suggested_covers`, `ai_confidence` | **UNKNOWN — never audited** | Already wired into the dashboards; daily cadence | Black box, vendor-locked, no weather/event/forward-book inputs Method controls, confidence flag tracked but not gated |
| **Naive baseline (same DOW prior week)** | DOW only | **~15-25% MAPE** typical for restaurant ops | Free, ten lines of SQL, no vendor | Misses weather, holidays, events, trend, payroll cycles |
| **Prophet / NeuralProphet** | History + holidays + future-known regressors (weather, events) | Restaurant studies: **~19.6% sMAPE one-day** out-of-the-box; NeuralProphet 50-90% better than Prophet with auto-regression | Open source, holiday-aware, additive components are explainable | Needs tuning; daily-only models miss intraday |
| **Nixtla statsforecast (AutoARIMA, AutoTBATS, AutoETS)** | Univariate + exogenous; 7-day weekly seasonality | ~22% MAPE on retail w/o regressors; 30x faster than pmdarima, 500x faster than Prophet | Best speed/accuracy ratio for univariate; cheap to run nightly | Univariate by default — needs feature engineering for weather/events |
| **Nixtla TimeGPT** | Pretrained on 100B+ data points across retail/finance/IoT; few-shot daily forecasts | Foundation-model class; competitive zero-shot on retail | No training data needed; API-call simple | Still an external API; less interpretable than Prophet |

## Industry accuracy bar

Daily sales-forecast MAPE bands consistently cited across vendor and academic sources:

| Tier | Daily MAPE | Source / typical operator |
|---|---|---|
| World-class (AI at scale, multi-input) | **<3%** | Crunchtime/5-Out claims; "world-class operations running AI-driven forecasting at scale" |
| High-performing | **3-7%** | "MAPE below 7% is high-performing"; MarginEdge at 4% |
| Acceptable | **7-15%** | Mid-pack chains; Prophet-class baselines |
| Spreadsheet/manual | **12-20%** | "manual spreadsheet-based forecasting typically produces MAPE in the 12 to 20% range" |
| Naive seasonal | **15-25%** | DOW-only baselines; also matches hotel-industry typical range per Lighthouse |
| Hotel rooms (peer comparison) | **<10% excellent · 10-20% acceptable · 15-25% typical** | Lighthouse |

For Method's mix:
- **Coffee shop (Little Wing):** very weather-sensitive, low cover count → expect higher % swings; 8-12% MAPE is good, sub-7% is hard.
- **FSR (Wm. Mulherin's, Lowland, Le Supreme, Hiroki-San):** reservation-driven; with Resy forward-book wired in, 5-7% is achievable.
- **Banquet / event (Anthology, Vessel, Quoin Rooftop):** Tripleseat BEO contracted revenue is *deterministic*, not a forecast — should be carried at face value, not modeled. This is a category Method is almost certainly leaving accuracy on the floor by passing through helixo-2's generic guess.
- **Hotel F&B (Kampers, ROOST coffee shops):** RevPAR + house occupancy from Mews is the dominant regressor. Industry hotel-rooms forecasts at 10-15% MAPE — F&B inside those hotels should match if rooms data is wired in.

## What Method's forecast is missing vs. the field

1. **Weather.** Crunchtime, 5-Out, Toast IQ all natively integrate weather. Method has no wired NOAA/Tomorrow.io feed influencing the daily forecast. Tomorrow.io aggregates NOAA + ECMWF and is used by Uber/JetBlue/Ford for demand work — drop-in for our scale.
2. **Events / holidays beyond federal.** PredictHQ (used via AWS Data Exchange by national QSR chains) reports +20% RMSE improvement and $2.7M supply-chain gains at one fast-casual; -14% food waste, +7% sales at another from event-aware staffing. Method has neither a sports calendar (76ers, Eagles, Phillies, Tigers, Ravens, Orioles, Pelicans nights) nor a concert/festival feed wired to forecast.
3. **Reservations forward book (Resy).** Method has Resy at every FSR but does not feed forward bookings into the forecast. This is the single highest-signal regressor for FSR forecasting and we're ignoring it. (See also: Resy live-sync auth limitation flagged in MEMORY — even seed-only data is better than zero.)
4. **Tripleseat contracted BEOs.** Banquet revenue 30 days out is *known*, not forecast. Should override helixo-2 on event days.
5. **Hotel occupancy (Mews) feed to in-hotel F&B.** ROOST occupancy is the strongest driver of Little Wing/Kampers covers. Not wired.
6. **Forecast accuracy tracking.** No MAPE/WAPE/bias ledger in place. We cannot answer "how good is helixo-2?" today.
7. **Confidence gating.** `ai_confidence` is recorded but doesn't trigger fallback to a sane baseline when it craters — meaning a low-confidence helixo-2 day still drives staffing.
8. **Retrainability.** helixo-2 is a passthrough. Method cannot inject local knowledge (a Mulherin's wedding this Saturday, a Tigers home opener at Le Supreme, Quoin closing for renovation) and have it improve the model.

## Recommendations — phased upgrade path

**Phase 1 — Build the accuracy ledger (in flight).** Persist `forecast_revenue` / `forecast_covers` snapshotted at T-7, T-1, and same-day; join to actuals; report MAPE/WAPE/bias by outlet, by DOW, by week. Until this exists, every other phase is faith-based. This is cheap, all-internal, 1-2 sprints.

**Phase 2 — Wire the regressors Method already owns.**
- Mews occupancy → in-hotel F&B (Kampers, Little Wing, hotel coffee shops).
- Resy forward bookings → FSR covers (Mulherin's, Lowland, Le Supreme, Hiroki-San, Rosemary & Rose, Quoin Rooftop).
- Tripleseat contracted BEOs → override forecast on event days (Anthology, Vessel, Mulherin's private events).
- Federal + Pennsylvania/Delaware/Michigan/Maryland/South Carolina state holiday calendar via `pandas-market-calendars` or `holidays` (Python, free).

**Phase 3 — External feeds.**
- Weather: Tomorrow.io free tier or NOAA NDFD (free, no key) — daily high/low/precip for each outlet ZIP.
- Events: PredictHQ or Eventbrite + manual sports calendars for Detroit/Philly/Charleston/Baltimore/Wilmington/Tampa/Cleveland.

**Phase 4 — Run a baseline model in parallel.** Stand up Prophet (or NeuralProphet, or Nixtla AutoARIMA — statsforecast is 500x faster than Prophet and free) as a *shadow forecast* alongside helixo-2. Same regressors from Phase 2-3. Track MAPE side-by-side for 60 days. Three outcomes:
- Shadow beats helixo-2 → switch primary, keep helixo-2 as second opinion.
- helixo-2 beats shadow → we now have proof, and an ensemble (average the two, weighted by recent accuracy) usually beats both.
- They tie → we have negotiating leverage on helixo-2 pricing and proof Method can self-host if needed.

**Phase 5 — Ensemble or replace.** Once Phase 4 has 60+ days of head-to-head, decide. MarginEdge's new forecast (already covering all 11 Method outlets, claimed 4% MAPE) is the lowest-friction commercial alternative — zero new vendor, already getting POS data.

## Vendor-lock risk

Method is in the worst quadrant of AI dependency: **high reliance, low transparency, no internal alternative.** Specific exposures:

- **Silent degradation.** helixo-2's model can drift, get retrained on bad data, or change behavior with a release we never see. With no accuracy ledger we'd find out from missed labor budgets, not from monitoring.
- **Pricing leverage.** Vendor knows we have no fallback. Renewal negotiations are one-sided.
- **Disappearance risk.** If helixo-2 sunsets the product, gets acquired, or jacks pricing, Method has no shadow model warmed up. Standing up Prophet/AutoARIMA on cold data takes a week; standing up *trusted* output takes a quarter.
- **Compliance/explainability.** EU AI Act (2025) and growing US state AI rules require risk assessments for high-impact AI. We can't explain why helixo-2 said what it said — we can explain Prophet to anyone.
- **Industry-wide pattern.** Per Hospitality Net, "98% of hotels lose revenue to rate misuse almost every four days" partly because AI agents are trapped in vendor silos. Method is one Tripleseat-helixo2 mismatch away from staffing a 220-cover Saturday for 90.

The single highest-ROI move is Phase 1 + a 30-line Prophet shadow forecast. Everything else flows from having the scoreboard.

---

**Sources**

- [5-Out features and accuracy claims (SaaSWorthy)](https://www.saasworthy.com/product/5-out-io)
- [5-Out: 7 Best Restaurant Forecasting Software](https://www.5out.io/post/7-best-restaurant-forecasting-software)
- [Crunchtime AI Forecasting](https://www.crunchtime.com/blog/how-crunchtimes-new-ai-forecasting-helps-restaurants-improve-profitability)
- [Crunchtime weather-driven AI forecasting](https://www.crunchtime.com/blog/introducing-weather-driven-ai-forecasting-for-restaurants)
- [Restaurant365 Sales Forecasting](https://www.restaurant365.com/workforce/sales-forecasting/)
- [MarginEdge expands AI suite — sales forecasting (Aug 2025)](https://www.businesswire.com/news/home/20250807784740/en/MarginEdge-Expands-AI-Suite-to-Transform-Restaurant-Operations-With-Sales-Forecasting-Recipe-Building-and-Invoice-Automation-Tools)
- [MarginEdge Sales Forecasts product page (4% MAPE claim)](https://www.marginedge.com/sales-forecast)
- [Toast IQ AI Assistant (2025)](https://pos.toasttab.com/news/toast-expands-toast-iq-smart-ai-assistant)
- [Toast 2025 AI in Restaurants Survey](https://pos.toasttab.com/blog/data/ai-in-restaurants)
- [Olo: 11 ways Olo uses AI](https://www.olo.com/blog/11-ways-olo-uses-ai-to-fuel-restaurant-growth)
- [IDeaS vs Duetto comparison (Epic-Rev)](https://www.epic-rev.com/post/ideas-vs-duetto-the-ultimate-showdown-of-revenue-management-systems-for-hotels)
- [Duetto: AI-powered future of revenue management](https://www.duettocloud.com/library/the-ai-powered-future-of-revenue-management-duetto)
- [Lighthouse: MAPE in hotels (10% excellent, 15-25% typical)](https://www.mylighthouse.com/resources/blog/mape)
- [BlackBox Intelligence](https://blackboxintelligence.com/)
- [Restaurant sales forecasting MAPE bands — Supy](https://supy.io/blog/learn-restaurant-sales-forecasting)
- [Machine Learning Based Restaurant Sales Forecasting (MDPI)](https://www.mdpi.com/2504-4990/4/1/6)
- [PredictHQ for QSR / restaurant forecasting](https://www.predicthq.com/industries/quick-service-restaurants)
- [PredictHQ + AWS Data Exchange food supply chain case](https://aws.amazon.com/blogs/awsmarketplace/food-supply-chain-optimization-using-predicthq-intelligent-event-data-from-aws-data-exchange-for-demand-forecasting/)
- [Tomorrow.io Weather API (NOAA + ECMWF aggregator)](https://www.tomorrow.io/weather-api/)
- [Prophet — seasonality, holidays, regressors](https://facebook.github.io/prophet/docs/seasonality,_holiday_effects,_and_regressors.html)
- [NeuralProphet GitHub](https://github.com/ourownstory/neural_prophet)
- [Nixtla statsforecast (AutoARIMA, 500x faster than Prophet)](https://github.com/Nixtla/statsforecast)
- [Nixtla TimeGPT](https://www.nixtla.io/)
- [AI vendor lock-in best practices (TechTarget)](https://www.techtarget.com/searchenterpriseai/tip/Best-practices-to-avoid-AI-vendor-lock-in)
- [Hospitality Net: AI agents trapped in vendor silos, 98% of hotels lose revenue to bad data](https://www.hospitalitynet.org/editorial/4131990/ai-agents-are-trapped-in-vendor-silos-98-of-hotels-lose-revenue-to-bad-data-the-gm-role-is-being-rebuilt-by-2030)
