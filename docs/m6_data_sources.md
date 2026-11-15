# End-of-day data sources for M6

Checked on 15 November 2026 on each vendor's own pricing, documentation and terms pages.
Prices are in USD, for personal (non-professional) use, as the vendor shows them. The user
picks one; §5.2 still needs a second, independent price source as the cross-check.

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
  (a second vendor, or IBKR for recent history only, which needs market-data subscriptions).

## Sources

- EODHD pricing: https://eodhd.com/pricing
- EODHD exchanges: https://eodhd.com/list-of-stock-markets
- EODHD splits and dividends API: https://eodhd.com/financial-apis/api-splits-dividends
- EODHD terms: https://eodhd.com/financial-apis/terms-conditions
- FMP pricing: https://site.financialmodelingprep.com/pricing-plans
- Alpha Vantage premium plans: https://www.alphavantage.co/premium/
- Alpha Vantage documentation: https://www.alphavantage.co/documentation/
- Alpha Vantage terms: https://alphavantage.co/terms_of_service
