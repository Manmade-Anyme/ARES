# TASK-010 — Latest ARES trade versus real option prices

Date: 6 October 2026. All times below are IST.
Scope: Latest generated trade at the start of this investigation, plus the latest
closed trade to inspect an exit. Read-only Supabase and Dhan queries were used.
Market candles cover the session through the 12:54 minute; trade states were
rechecked at 12:57:39. This is a fixed observation window, not a live report.

## Sources and contract identity

- Supabase project `Trading signal data`: joined `trade_analytics`, `ares_signals`,
  `active_trades` and the UUID-bound `ml_collection` signal snapshots.
- The signal snapshots report DTE=0 and expiry day on 6 October. Dhan's chain for
  **2026-10-06** resolves the exact contracts to **40710 (22,700 CE)** and
  **40703 (22,600 PE)**. Broker trade-book metadata also confirms the PE expiry.
- Dhan token and active data subscription were checked through the existing
  credential loader. Credentials were never printed or saved in audit artifacts.
- Fetched 219 one-minute candles each for NIFTY spot and both exact option
  contracts, then joined them by their epoch timestamps.
- Current-day broker trade book contains no 40710 fills and no 40703 fills in
  the generated trade's 09:42–09:46 window. Other 40703 fills start at 10:17:03;
  they cannot be attributed to this generated trade. No actual broker entry/exit
  pair was identified for either signal.

## Latest generated trade: display #1127 / signal DB ID 545

Trade UUID: `ec91a4ee-037f-43ce-9b87-662914841f49`
Signal UUID: `5680647b-bdcd-408e-9a21-d736eb2431ef`
Setup: bullish exhaustion reversal, 22,700 CE, expiry 6 October 2026.

| Item | Recorded value |
| --- | ---: |
| Signal candle timestamp | 12:31:00 |
| Signal persisted | 12:31:24.659 |
| Trade registered | 12:31:24.851 |
| Spot entry reference | 22,693.20 |
| Original spot SL | 22,683.20 |
| Spot T1 | 22,711.20 |
| Spot T2 | 22,733.20 |
| Option entry LTP reference | Rs 39.95 |
| Selected contract delta | +0.48373 |
| Calculated option SL | Rs 35.11270 |
| Calculated option T1 | Rs 48.65714 |
| Suggested lots | 1, using configured quantity 65 |

The current code derives premium levels from the symmetric entry-zone midpoint,
which equals the trigger price here:

```text
Spot SL distance = 22693.20 - 22683.20 = 10
Spot T1 distance = 22711.20 - 22693.20 = 18
Option SL = 39.95 - 10 * 0.48373 = 35.11270
Option T1 = 39.95 + 18 * 0.48373 = 48.65714
```

Capital was 7,969.17; the 4% risk budget was 318.7668. Estimated loss per
65-unit lot was 314.4245, so risk permits one lot. Premium cost per lot is
2,596.75, so affordability permits three. The calculator selects one lot.
No option T2 is calculated by production code.

The recorded Rs 39.95 entry is the fetched chain LTP, not a calculated entry or
a verified broker fill. It lies within the real 12:31 option candle's 39.20–41.70
range. That full minute closes at 40.25; it is not the same observation as the
mid-minute signal snapshot at approximately 12:31:24.

### Observed performance through 12:54

- The first full spot candle touching T1 is **12:38**, with high 22,711.35.
  Its option candle has open 42.45, high **53.55**, low **41.70**, close **50.75**.
  The calculated 48.65714 target falls inside that range. We cannot identify the
  exact option premium at the instant spot crossed T1 from these minute bars.
- From 12:31 through 12:54, the option's observed low is **36.10** and high
  **64.00**. The original projected option SL 35.11270 was not reached in this
  window. Spot T2 and the original spot SL were also not touched in these bars.
- The 12:54 option close is **47.45**. Relative to the recorded 39.95 entry
  reference, this is **+7.50 per unit (+18.77%)**, or **+487.50 for 65 units**,
  before charges and execution spread. This is a hypothetical mark-to-reference
  change, not realized account P&L.
- A chain snapshot retrieved at **12:54:57** shows LTP **47.55**, bid **47.55**,
  ask **47.65**. These are a separate, later observation than the minute close.
- At the 12:57:39 state check, `active_trades.state` is **T1_HIT**, with current
  spot SL moved to **22,693.20**. There is **no recorded exit**. The analytics
  row remains OPEN until terminal exit; these two state fields serve different
  purposes. A spot break-even stop does not establish premium break-even.

## Latest closed trade: display #1632 / signal DB ID 544

Trade UUID: `de816ed1-0615-4f2f-b963-6186aa6cdba6`
Setup: bearish failed breakout, 22,600 PE, expiry 6 October 2026.
Signal candle: 09:42:00; persisted at approximately 09:42:33.

```text
Spot entry = 22614.80
Spot SL = 22626.80  -> 12-point adverse move
Spot T1 = 22594.80  -> 20-point favourable move
Option entry reference = 50.30
Delta = -0.45084
Option SL = 50.30 - 12 * 0.45084 = 44.88992
Option T1 = 50.30 + 20 * 0.45084 = 59.31680
Predicted SL loss for 65 units = 5.41008 * 65 = 351.6552
```

The actual option entry-minute range is 46.65–51.00, which contains the recorded
50.30 LTP. The whole-minute close is 49.45, not an exact entry fill.

ARES records **SL_HIT at 09:46**, exiting in spot terms at **22,626.80** for
**-12 spot points**. This exit price is the crossed spot level stored by the
position manager, not an observed premium fill. The spot 09:46 candle reaches
high 22,627.30, independently confirming the SL touch.

The exact PE's 09:46 option candle is:

| Open | High | Low | Close |
| ---: | ---: | ---: | ---: |
| 47.85 | 48.55 | 45.60 | 48.55 |

Thus **the projected option SL 44.88992 is below the entire option candle's
observed range even though spot SL triggered**. The estimated premium loss of
351.66 per lot does not match the observed premium range in that exit minute.
Marking the assumed entry against this minute's traded-price range gives losses
of **113.75–305.50 for 65 units**, before charges; marking at the minute close
alone gives **-113.75**. Neither is an exact execution result, and bid/ask fills
may differ from traded-price OHLC. T1 was not touched before the spot exit.

## What this tells us about improvements

The arithmetic matches the stored signal values. These examples expose the
limitations of translating spot distances with frozen entry delta rather than
establishing that a premium SL is an executable equivalent of a spot SL.

Entry Greeks for the latest CE are gamma 0.00377, theta -61.51848, vega 2.84139,
IV 14.83487. Adding a local gamma correction under unchanged time/IV yields
**SL 35.30120 and T1 49.26788**, versus current 35.11270/48.65714. This is a
conditional projection; it is not validated by comparing it directly with the
T1-minute close, whose spot close is different from the exact T1 level.

For a matched-close comparison at 12:38, spot closes at 22,709.25. The entry-delta
estimate is **47.71387**, delta+gamma **48.19945**, observed option close **50.75**.
Gamma narrows this particular residual but leaves a material difference. One
minute is not sufficient evidence that the model generalizes.

For the closed PE, the entry gamma is 0.00269: gamma changes the projected SL
only from **44.88992 to 45.08360**. The stored ATM PE IV also changes from
**18.00634 at entry to 18.48909 at the 09:46 snapshot**, while both snapshots are
for the 22,600 ATM strike. IV is demonstrably not frozen. These observations
support evaluating quote timing, gamma and IV together; they do not isolate a
causal contribution or forecast future IV. Theta time units need provider
verification before quantitatively decomposing time decay.

Recommended next evaluation: synchronized selected-contract quote/spot snapshots,
explicit quote age and spread, consistent SL/T1/T2 scenarios, then compare pricing
error across more trades and expiry distances. Keep spot exits authoritative
unless a separate strategy change is approved. The existing two observations do
not establish improved profitability or justify larger position sizes.

## Files and reproducibility

- [Price chart](TASK-010_latest-option-audit-2026-10-06.png)
- Audit inputs, exact-contract minute OHLC, sanitized relevant broker fills and
  matched-close calculations: `scratch/option-audit-20261006/`.
- Read-only retrieval scripts: `fetch_market.py` and `fetch_fills.py`; analysis:
  `analyze.py`. They use the existing credential loader without saving credentials.
- Full entry-minute bars contain pre-publication observations; the quote at the
  exact publication/spot-touch instant cannot be reconstructed from minute OHLC.
- No orders or database rows were written; production trading code unchanged.
- [Dhan historical candle semantics](https://dhanhq.co/docs/v2/historical-data/)
- [Dhan option chain fields](https://dhanhq.co/docs/v2/option-chain/)
