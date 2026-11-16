# End-of-day data sources for M6

**Chosen primary (v1.9 review): EODHD, EOD Historical plan.** The key goes in the gitignored
`.env` as `VENDOR_API_KEY=...` (§18); it is read from there and never printed. Then:

    himari data coverage

checks each of the nine funds (prices, splits, distributions) from its listing date in
`config/fund_inceptions.yaml` and lists every gap. It needs about 28 API calls (3 per fund and
1 symbol list to resolve CGL.C) and caches every response under `data/raw/eodhd/`, so a rerun
spends nothing. EODHD's free plan gives 20 calls a day and one year of history, so the
from-inception check needs the paid plan; on the free plan every fund would show a late start.

## Cross-check source (§5.2): options for the user to pick

Checked on 15 November 2026 on each site's own pages and terms.

**Chosen (v1.10 review): IBKR daily bars for all nine funds**, once the account and its TSX and
NASDAQ Level 1 data are live. The issuers' pages are used by hand only, for Q10 (the list of
files and the folder are in `src/himari/data/issuer_files.py`).

| Option | What it gives | Cost | Terms (personal use) | Gaps |
|---|---|---|---|---|
| IBKR daily bars (TWS API), once the account exists | Exchange OHLCV for all nine funds, independent of EODHD; daily bars up to 68 years per request, pacing limits lifted for 1-minute bars and larger | Toronto Stock Exchange L1 CAD 9.00/month and NASDAQ L1 USD 1.50/month (non-professional; footnoted eligibility on IBKR's page) | Market data under IBKR's non-professional subscriber terms | History depth is not documented: checked per fund with `reqHeadTimestamp`; no distribution history |
| Issuers' own pages | Vanguard (VCN, VUN, VIU, VEE, VGV): daily NAV and market price download ("Download 3,009 prices" for VCN, VUN and VEE, about 12 years; 2,749 for VIU and 2,457 for VGV) and a distribution history with separate cash and reinvested columns. BlackRock (CGL.C): historical NAV xls. Global X (DLR, CBIL): distribution table, NAV chart only. Invesco (PDBC): an "Export data" button | Free | All: personal, non-commercial. Vanguard: downloading and storing for personal use allowed; automated access not addressed. BlackRock: robots, scripts and "automated devices" prohibited without permission. Invesco: no downloading beyond page caching; robots and scraping prohibited. Global X: all rights reserved; its pages refuse scripted requests | NAV is not the market close (BlackRock); no price download for Global X or Invesco; manual downloads only |

The issuers' distribution histories also serve Q10: Vanguard and Global X show the cash and
reinvested parts of each December distribution.

## Vendor comparison (15 November 2026)

Checked on each vendor's own pricing, documentation and terms pages. Prices are in USD, for
personal (non-professional) use, as the vendor shows them.

| | EODHD | Financial Modeling Prep (FMP) | Alpha Vantage |
|---|---|---|---|
| Plan covering TSX and NASDAQ end-of-day | EOD Historical ("All-World") | Premium adds "UK and Canada Coverage"; Ultimate adds global coverage | Any premium plan (the adjusted daily series is premium-only) |
| Price per month | $19.99 (annual billing: 2 months off) | Premium $69 ($49 billed annually); Ultimate $139 ($99 billed annually) | $49.99 (75 requests/min) up to $249.99 (1,200 requests/min); annual plans 2 months off |
| History depth (vendor's statement) | EOD "30+ yrs"; US from 1972 | Premium "up to 30 years"; Ultimate "Full Historical Access" | "25+ years" |
| API limits | 100,000 calls a day, 1,000 a minute; 1 call per end-of-day request | Premium 750 calls a minute, Ultimate 3,000; bandwidth 50 GB (Premium) or 150 GB (Ultimate) per trailing 30 days | 75 to 1,200 requests a minute, no daily limit (free: 25 a day) |
| TSX symbols | Exchange code `TO` (XTSE), e.g. `VCN.TO` | Canada on Premium and above | Suffix `.TRT`, e.g. `SHOP.TRT` |
| Distributions | `/api/div/`: `date`, `declarationDate`, `recordDate`, `paymentDate`, `period`, `value`, `unadjustedValue`, `currency` | "Earnings, Dividends, Splits": "Limited Coverage" on Premium, "Full Coverage" on Ultimate | "historical split/dividend events" in the daily adjusted series |
| Splits | `/api/splits/`: `date`, `split` | as above | as above |
| Reinvested (non-cash) distributions marked? | No (only `period`, e.g. "Special") | Not stated | No |
| Licence for personal use | Non-professional users may store, adjust and analyze the data for their own non-commercial purposes; no sharing or redistribution | Personal Use plans; displaying or redistributing needs a Data Display and Licensing Agreement | Personal, non-commercial use unless agreed otherwise in writing |
| Caveats | The site's disclaimer says its ETF prices are not from exchange feeds and may differ from the market price | Premium's "limited coverage" of dividends, splits and ETFs: ask FMP whether all nine funds are fully covered, or take Ultimate | Premium needed for adjusted data |

## What holds for every vendor

- None marks a reinvested (non-cash) year-end distribution, so the Q10 confirmations against
  the issuers' announcements (§6, v1.6) stay necessary whichever vendor is picked.
- The vendors state history depth per plan, not per symbol. Whether each of the nine funds
  (the eight ETFs and CBIL) is covered from its own inception has to be checked symbol by
  symbol on a trial or first-month key before the baseline. None is assumed here.
- §5.2 needs two independent price sources: the chosen vendor as primary and a cross-check
  (a second vendor, or IBKR's daily bars, which need market-data subscriptions; *(v1.10)* IBKR
  was chosen).

## Sources

- EODHD pricing: https://eodhd.com/pricing
- EODHD exchanges: https://eodhd.com/list-of-stock-markets
- EODHD splits and dividends API: https://eodhd.com/financial-apis/api-splits-dividends
- EODHD terms: https://eodhd.com/financial-apis/terms-conditions
- FMP pricing: https://site.financialmodelingprep.com/pricing-plans
- Alpha Vantage premium plans: https://www.alphavantage.co/premium/
- Alpha Vantage documentation: https://www.alphavantage.co/documentation/
- Alpha Vantage terms: https://alphavantage.co/terms_of_service
- EODHD end-of-day API: https://eodhd.com/financial-apis/api-for-historical-data-and-volumes
- EODHD exchanges and tickers API: https://eodhd.com/financial-apis/exchanges-api-list-of-tickers-and-trading-hours
- Vanguard Canada products and terms: https://www.vanguard.ca/en/product, https://www.vanguard.ca/en/website-terms-of-use
- BlackRock Canada CGL.C and terms: https://www.blackrock.com/ca/investors/en/products/241528/ishares-gold-bullion-etf, https://www.blackrock.com/ca/investors/en/terms-and-conditions
- Global X Canada DLR and terms: https://www.globalx.ca/product/dlr, https://www.globalx.ca/terms-of-use
- Invesco PDBC and terms: https://www.invesco.com/us/en/financial-products/etfs/invesco-optimum-yield-diversified-commodity-strategy-no-k-1-etf.html, https://www.invesco.com/us/en/resources/terms-of-use.html
- IBKR Canada market data pricing: https://www.interactivebrokers.ca/en/pricing/market-data-pricing.php
- IBKR TWS API historical limitations: https://interactivebrokers.github.io/tws-api/historical_limitations.html
