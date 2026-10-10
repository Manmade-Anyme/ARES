# TASK-010 — Option entry, SL and target calculation review

Date: 2026-10-06
Status: Investigation complete; improvement options proposed, no production changes.
Scope: Current ARES checkout. Indexed and traced with ai-grep. This reviews the
options reference layer and identifies the spot geometry from which it derives.

## Current calculation

`main.py:320-338` fetches a spot candle, nearest-expiry option chain and structural
levels, then asks the engine for a signal. `engine.py:19-47` centrally applies the
following fixed spot distances from the signal's trigger price. Both expiry and
non-expiry profiles currently use the same table (`config_profiles.py:38-43`).

| Setup | SL distance | T1 distance | T2 fallback distance |
| --- | ---: | ---: | ---: |
| Exhaustion reversal | 10 | 18 | 40 |
| Trend continuation | 25 | 25 | 80 |
| OI wall rejection | 16 | 25 | 40 |
| Failed breakout | 12 | 20 | 55 |

For bullish trades, SL is below entry and targets above; bearish trades reverse
the signs. T2 uses the nearest structural level beyond T1 in the favourable
direction, if one exists, otherwise the table's fallback distance. Structural
T2 is not capped by that fallback distance. The engine checks spot T1 reward/SL
risk against the configured minimum ratio of 1.0.

Detectors create an entry zone of trigger price +/- 5 spot points. The option
calculator uses the midpoint, which equals the trigger in these symmetric zones.
This is a displayed spot zone, not a calculated option-premium entry range.
PositionManager records the spot passed by main when registering the trade;
there is no broker fill price in this calculation.

`options_math.py:48-145` then:

1. Fetches available Dhan balance, falling back to configured capital on failure.
2. Chooses CE for bullish, PE for bearish; accepts absolute delta 0.45 through
   0.55 and chooses the **lowest** absolute delta within that band.
3. Uses that contract's chain `last_price` as the option entry reference.
4. Computes `SL_move = abs(entry_mid - spot_SL) * abs(delta)` and
   `T1_move = abs(spot_T1 - entry_mid) * abs(delta)`.
5. Publishes `option_SL = max(1, LTP - SL_move)` and
   `option_target = LTP + T1_move`. There is no option T2 projection.
6. Suggests the smaller of `floor(capital * risk_pct / 100 / (SL_move * lot_size))`
   and `floor(capital / (LTP * lot_size))`.

Current configured risk percentage is 4%, fallback capital is Rs 100,000, and lot
size is 65. These are repository settings, not a verification of exchange contract
metadata or the user's running deployment.

Example, using hypothetical quotes for OI wall rejection: LTP 100 and delta 0.50
give SL 92 and T1 112.50, from spot distances 16 and 25.

`README.md:89-100` expressly makes spot levels authoritative. `position_manager.py:
327-369` evaluates spot SL/T1/T2 and moves the spot SL to spot entry after T1.
Changing premium estimates would not itself change signal detection or exits.

## Findings, in priority order

### 1. Missing market data produces apparently usable prices and lots

`options_math.py:96-112` substitutes LTP 100 and delta +/-0.50 when the selected
fallback contract's fields are missing. This fabricated quote is presented like
a real quote. With capital 100,000, the OI-wall example yields seven lots even
when both quote and delta are missing. The existing missing-metrics test
explicitly expects these defaults, so passing tests do not establish quote validity.

Proposed behaviour: keep the spot signal, mark option estimates/sizing unavailable
when required data is unobserved, and distinguish a labelled model estimate from
a market quote. Validate finite positive prices, valid delta sign/range, contract
identity/expiry, and quote age before publishing a usable option suggestion.

### 2. A successful zero balance becomes fallback capital

`options_math.py:39` uses `availabelBalance or availableBalance`; zero is therefore
discarded. A successful payload containing only `availabelBalance: 0` reaches
the fallback and returns 100,000. This was reproduced locally with a mock client.

Proposed behaviour: use explicit missing-value checks, preserve zero, validate
capital, and label fallback capital as hypothetical rather than current funds.

### 3. LTP is a reference, not an executable entry quote

The current selection has no spread, depth, liquidity or freshness checks. Dhan
provides best bid/ask and quantities, but the current normalizer does not carry
them into `full_chain`. Main fetches the candle before the chain and awaits funds
before publishing the suggestion, so the code does not establish a synchronized
spot/option reference at publication time.

Proposed behaviour: retain contract identity, expiry, snapshot spot, timestamps,
bid/ask and depth. For a purchase, show a current ask-based entry estimate and
record the actual fill if an execution integration is later in scope. Ask-based
entry remains an estimate subject to depth and price changes. Project theoretical
mid-value changes separately from entry ask and exit bid assumptions; adding a
Greek move directly to an entry ask does not model the round-trip spread.

### 4. The Rs 1 SL floor can violate basic ordering

The selector accepts LTP 0 or 0.50 with a valid delta. For LTP 0.50 and delta 0.50,
spot SL distance 16 produces option SL 1, target 13 and seven suggested lots at
capital 100,000: the SL is above entry. These inputs were reproduced locally.
Raw loss used for sizing also differs from the loss implied by a clamped SL.

Proposed behaviour: validate inputs and projected ordering, handle model validity
bounds explicitly, and use instrument tick metadata for executable price rounding.
Do not silently turn an invalid projection into a valid-looking order level.

### 5. Delta-only pricing omits curvature, time and IV assumptions

The formula freezes delta. A local second-order, unchanged-time/unchanged-IV
projection for a long vanilla option would be:

`P(level) ~= P0 + delta * (S_level - S0) + 0.5 * gamma * (S_level - S0)^2`

Here S0 must be the underlying price associated with the quote/Greeks snapshot;
use signed delta and spot movement so both calls and puts work. Apply this to SL,
T1 and T2 consistently. Gamma is positive for long calls and puts: the correction
adds to favourable premium gains and reduces the spot-only adverse premium loss,
all else equal. This is an approximation, not a safe loss bound for sizing.

For the hypothetical OI-wall example above, with gamma 0.002, the projection gives
SL 92.256 and T1 113.125 rather than 92 and 112.50. This illustrates magnitude;
it is not observed trading performance. The correction may be small relative to
spread or quote-age error at current short T1 distances.

Elapsed time and changing IV also alter premium. Since time to reach a spot level
is unknown, show explicit time/IV scenarios or retain a clearly labelled
instantaneous, unchanged-IV estimate. Do not invent a holding period. Verify the
provider's theta time units and vega IV units before using them. For larger moves
or near expiry, reprice under an explicit model instead of trusting a constant
Greek Taylor expansion outside its local range.

### 6. Existing V2 prototype needs integration and validation work

`scratch/poc_option_calculator_v2.py` is not called by main. It ranks contracts by
distance from absolute delta 0.50 with only a lower bound of 0.45, adds gamma to
T1 only, keeps delta-only SL/sizing, and retains the Rs 1 floor. It also supplies
invented defaults for gamma/theta/vega. Current `full_chain` lacks those fields:
`fetchers/oi_fetcher.py:295-311` exports LTP, IV and delta; the other Greeks are
retained only in ATM OptionRow objects. A non-ATM selected contract therefore
cannot receive its actual gamma from the existing calculator input.

Selecting closer to 0.50 is a policy alternative, not proven superior to the
current deliberate 0.45 preference. Compare candidate liquidity, price, exposure
and out-of-sample results before adopting a different strike policy.

## Improvement options

| Option | Benefit | Limitation |
| --- | --- | --- |
| A. Validated delta projection | Small change; fixes fabricated data, quote basis, invalid levels and capital handling | Still a local linear approximation |
| B. Validated delta + gamma projection | Uses selected contract's curvature; consistent SL/T1/T2 references | Local, conditional model; needs full-chain Greeks and validity checks |
| C. Full model repricing across time/IV scenarios | Handles larger movements and explicit horizons more coherently | More inputs, calibration and model assumptions; not a price forecast |

Recommendation: implement A first, then evaluate B against A using synchronized
selected-contract quotes. Consider C where local approximation error justifies
it. Preserve spot-based exits and performance accounting. Do not increase lot
count merely because a gamma correction predicts smaller loss at spot SL;
actual premium risk also depends on time, IV, gaps and execution costs.

Historical spot outcomes alone cannot validate premium forecast accuracy because
ARES deliberately does not record exit premium. A separately scoped quote study
would compare estimates with observed selected-contract quotes at spot level
touches, by expiry distance, setup, move size and quote quality. Track price error,
coverage and invalid-data rates; do not equate improved price estimates with
improved strategy profitability.

## Verification

- `python3 -m pytest tests/unit/test_options_math.py tests/unit/test_per_type_levels.py -q`
  — 36 passed.
- Ephemeral offline mock harness reproduced lowest-delta selection, missing-quote
  fabrication, zero/sub-Rs-1 premium invalid SL ordering, and zero-balance fallback.
- No live API/account access, orders, production code changes or profitability
  backtest were performed. Existing unrelated working-tree edits were preserved.

## Primary sources checked

- [Dhan option chain fields and definitions](https://dhanhq.co/docs/v2/option-chain/)
- [OIC: Gamma](https://www.optionseducation.org/advancedconcepts/gamma)
- [OIC: Theta](https://www.optionseducation.org/advancedconcepts/theta)
- [OIC: Vega](https://www.optionseducation.org/advancedconcepts/vega)

All proposals remain exploratory; no architecture/implementation decision has
been finalized for subsequent documentation sync.
