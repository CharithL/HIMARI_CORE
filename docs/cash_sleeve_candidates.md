# Cash sleeve candidates (§7.4, amendment v1.7)

Checked on 14 November 2026 on each issuer's own product page. Figures are as the issuer shows
them, with the issuer's as-of dates. The user picks the fund; until then `cash_sleeve.symbol`
stays null and idle cash stays at the broker.

No issuer page publishes a typical bid-ask spread, so none is given here. Before the fund goes
into a run, its half spread must be measured from quotes like any other fund's (§11) and added
to `costs_v1.yaml`; the engine refuses to run the sleeve without one.

| | CBIL | TBIL | CMR | ZMMK | CGOV |
|---|---|---|---|---|---|
| Fund | Global X 0-3 Month T-Bill ETF | Harvest Canadian T-Bill ETF | iShares Premium Money Market ETF | BMO Money Market Fund, ETF Series | Hamilton Canadian T-Bill ETF |
| Holds | Government of Canada T-bills, remaining maturity generally under 3 months; 100% government (30 Oct 2026) | Government of Canada T-bills; six bills maturing 4 Nov 2026 to 13 Jan 2027, weighted maturity 0.10 years (30 Oct 2026) | Money-market paper: commercial paper 30.9%, financial CP 29.2%, other notes 11.9%, asset-backed CP 11.1%, CDs 7.2%; Government of Canada 3.5%; 211 holdings (12 Nov 2026). ABCP allowed since 7 Feb 2025 | Money-market instruments of Canadian governments and corporations (T-bills, bankers' acceptances, commercial paper), under 365 days each, average term under 90 days | Government of Canada T-bills, generally under 3 months |
| Inception | 12 April 2023 | 16 January 2024 | 19 February 2008 | 29 November 2021 | Not trading: anticipated launch 16 November 2026 |
| Management fee | 0.10% | 0.10% | 0.12% (0.25% before 12 January 2023) | 0.12% | 0.05% |
| MER | 0.11% (30 Jun 2026) | not shown | 0.13% | 0.13% | not shown |
| Distributions | Monthly (e.g. ex 29 Oct, paid 6 Nov 2026) | Monthly | Monthly | Monthly | Monthly (dates TBD) |
| Net assets | CAD 2.99 bn (12 Nov 2026) | CAD 193.9 m (13 Nov 2026) | CAD 2.59 bn (13 Nov 2026) | CAD 6.27 bn (13 Nov 2026) | — |
| Duration | 0.16 (weighted average, 31 Oct 2026) | — (weighted maturity 0.10 years) | 0.18 years effective (12 Nov 2026) | not shown | — |
| Typical spread | not published (average daily volume 694,516 units over 12 months) | not published | not published | not published | — |
| Other units | CBIL.U (USD), CBIL.L (CAD accumulating) | — | — | — | — |

## What the choice trades off

- Only CMR covers the 2015–2025 evaluation window, but it is a corporate money-market fund
  (commercial paper, including asset-backed paper), not government T-bills.
- The government T-bill funds are recent: CBIL from April 2023, TBIL from January 2024, CGOV
  not yet trading. Under §7.4 the sleeve is used only from the fund's own inception, so with a
  T-bill fund the baseline holds idle cash at the broker until 2023 or 2024.
- ZMMK (November 2021) sits between the two: mixed government and corporate paper.

## Sources

- CBIL: https://www.globalx.ca/product/cbil
- TBIL: https://harvestportfolios.com/etf/tbil
- CMR: https://www.blackrock.com/ca/investors/en/products/239414/ishares-premium-money-market-etf
- ZMMK: https://bmogam.com/ca-en/products/exchange-traded-fund/bmo-money-market-fund-etf-series-zmmk/
  and https://bmogam.com/ca-en/products/exchange-traded-funds/cash-alternatives
- CGOV: https://hamiltonetfs.com/etf/cgov/
