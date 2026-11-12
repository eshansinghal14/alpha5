# Coin Race pricer: vol diagnosis and fix

## Bottom line

- The vol model works. Rebuilt end to end on Coinbase order-book data, it is calibrated on two held-out days. Its fair values match or beat the Kalshi market's log loss against settlement in every bucket from 1 to 15 minutes before close.
- The ETH under-pricing comes from the inputs the live pricer feeds the model. HYPE's sd is about 5× too large, ETH's about 2×, and BTC's about 1.5×.
- Rebuilt with correct vol, your logged tick (KXCRYPTOLEAD15M-26OCT080015 with 275 s left) prices ETH at 0.849, against a market mid of 0.842. ETH won.
- The HYPE series feeding your features and correlations is noisy or broken. A clean Coinbase HYPE series correlates 0.6–0.7 with BTC on 30–60 s returns, while yours shows 0.07–0.25.

## Evidence

### 1. Reference pipeline vs Kalshi (Sep 1 and Oct 1 2026, held out of the HAR fit)

Data:
- Coinbase `book_snapshot_25` from Tardis, sampled every second.
- Kalshi 1-minute candles (bid/ask) and settled results.
- 151 events, 1,611 event-minute snapshots where all 5 markets had quotes.

The settlement check uses a Coinbase top-of-book 60 s TWAP. It picks the Kalshi winner 94.3% of the time on Oct 1 and 97.9% on Sep 1. Every miss was decided by less than about 1 bp.

Log loss against settlement (lower is better):

| Time to close | n | Market mid | Reference (t ν=5) | Ref + your 0.88/0.03 shrink | Your vol inflation | Inflation + your corr | No corr |
|---|---|---|---|---|---|---|---|
| ≤1 min | 60 | 0.383 | **0.341** | 0.432 | 0.549 | 0.581 | 0.449 |
| 1–3 min | 114 | 0.660 | **0.629** | 0.674 | 0.932 | 0.955 | 0.834 |
| 3–7 min | 436 | 1.074 | **1.036** | 1.053 | 1.213 | 1.251 | 1.195 |
| 7–15 min | 1001 | 1.367 | 1.366 | 1.365 | 1.442 | 1.485 | 1.444 |
| All | 1611 | 1.201 | **1.186** | 1.197 | 1.311 | 1.350 | 1.296 |

- "Your vol inflation" multiplies the sds by the ratios measured at your tick: BTC 1.5, ETH 2.1, SOL 1.2, XRP 1.0, HYPE 5.3.
- The market's loss minus the reference's is 0.015 nats, with a bootstrap 90% CI of [−0.002, 0.034] by event.
- On each day separately: Sep 1 market 1.170 vs reference 1.149, and Oct 1 market 1.234 vs reference 1.226.

Calibration of the reference (mean predicted vs realized frequency):

| Bin | 0–5% | 5–15% | 15–30% | 30–50% | 50–70% | 70–85% | 85–100% |
|---|---|---|---|---|---|---|---|
| Reference | 1.9/2.1 | 9.8/10.3 | 21.7/21.8 | 38.3/40.3 | 58.9/54.7 | 76.4/66.5 | 93.8/90.9 |
| Market | 3.0/2.1 | 9.4/9.3 | 21.3/22.3 | 38.4/39.6 | 58.8/56.6 | 76.6/70.8 | 90.6/96.7 |

HAR calibration on the held-out days, as mean(r²)/mean(f) at h = 300 s with robust features:

| Day | BTC | ETH | SOL | XRP | HYPE |
|---|---|---|---|---|---|
| Oct 1 | 0.83 | 0.86 | 0.89 | 0.87 | 0.98 |
| Sep 1 | 0.88 | 0.85 | 0.87 | 0.89 | 0.91 |

Your live values were BTC 0.34, ETH 0.29, SOL 0.66, XRP 0.35, HYPE 0.06.

### 2. Your logged tick, rebuilt

Setup: 04:10:25 UTC on Oct 8, x taken from your log, HAR features approximated from Coinbase 1-minute candles, correlation from 4 hours of 1-minute returns.

| | BTC | ETH | SOL | XRP | HYPE |
|---|---|---|---|---|---|
| Your sd (%) | 0.147 | 0.236 | 0.141 | 0.155 | 0.639 |
| sd implied by the trailing 1 h realized (%) | 0.096 | 0.110 | 0.120 | 0.155 | 0.121 |
| Reference HAR sd (%) | 0.115 | 0.136 | 0.144 | 0.170 | 0.137 |
| Fair, reference vol | 0.048 | **0.849** | 0.000 | 0.009 | 0.094 |
| Fair, your sd | 0.134 | 0.479 | 0.003 | 0.014 | 0.371 |
| Fair, your sd with HYPE fixed | 0.124 | 0.709 | 0.002 | 0.011 | 0.154 |

The market mid was 0.842, and ETH settled yes.

### 3. Correlation is a symptom, not a parameter to tune

HYPE vs BTC correlation of Coinbase mid returns:

| Data | 1 s | 10 s | 30 s | 60 s |
|---|---|---|---|---|
| Oct 1 (all coins) | 0.40 | 0.56–0.58 | 0.61–0.63 | 0.62–0.64 |
| Live recording, Oct 8, about 25 minutes | 0.33–0.45 | 0.72–0.77 | 0.70 | 0.71 |

Adding idiosyncratic noise to a coin's series inflates its variance and dilutes its correlations together. A 16× variance inflation cuts correlation by about 4× (0.6 → 0.15), which matches what you measured.

### 4. Which defect reproduces your symptoms (fault injection on Oct 1 HYPE)

| Injected defect | Forecast inflation (raw 1 s features) | Inflation (robust 10 s features) | Forward 300 s r² ratio | Corr vs BTC, 60 s |
|---|---|---|---|---|
| Clean | 1.0 | 1.0 | 1.00 | 0.62 |
| IID noise, 2 bp | 6.4 | 1.6 | 1.02 | 0.59 |
| IID noise, 4 bp | 20.2 | 3.1 | 1.07 | 0.52 |
| Reverting 30 bp 1 s glitches (0.5% of seconds) | 5.7 | 1.5 | 1.02 | 0.59 |
| Reverting 30 bp 5 s glitches | 5.7 | 3.3 | 1.10 | 0.52 |
| Stale 30 s freezes | 1.0 | 1.0 | 1.00 | 0.60 |
| 60 s one-sided-book jumps of 50 bp | 2.5 | 2.1 | 1.42 | 0.37 |

High-frequency noise of about 3–4 bp on your HYPE series reproduces the roughly 16× forecast inflation while your forward 300 s check still looks clean. To get correlations as low as 0.1–0.2 at 60 s, the series also needs noise that persists for tens of seconds. That fits a composite flipping between price levels, for example when the utilized depth or size cap toggles, or a mix of products or venues.

## Fix

1. **Find out what's wrong with the HYPE (and ETH) input series.** Run `python diagnose_bars.py your_bars.parquet` on your persisted bars. It needs columns `ts, coin, price` plus `mid` = raw Coinbase top-of-book mid if you have it. On a clean feed you should see:
   - signature RV(1)/RV(60) of 0.9–1.5,
   - |price − mid| rms of 1.5 bp or less,
   - HYPE vs BTC correlation of 0.55–0.75 at 30–60 s,
   - mean(r²)/mean(f) of 0.85–1.0.

   Likely causes, in order: the CF-replica weighting jumping on thin HYPE depth, bar-loop artifacts (stalls, values from different seconds, duplicate or mixed sources), and the wrong product or a stale book after resubscribing.
2. **Compute HAR features and correlations from the raw Coinbase top-of-book mid, not the CF replica.** The mid matches settlement winners as well as the replica (93–98%), and it has no extra noise. If you keep the replica, use it only for x and the TWAPs.
3. **Use the robust features in `rv2.rv_features(p, k=10, clip=6)` with the matching `har_coefs.json`.** Squared 10 s returns are averaged over every 1 s offset, and each return is clipped at 6× a lagged EWMA sd. This is free on clean data (+0.2–0.3 held-out NLL gain) and much safer on bad ticks: a 30 bp glitch inflates the forecast 1.5× instead of 5.7×. The coefficients are refit for h = 30/60/120/300/600/900 s, so interpolate the rate in log h at h = time remaining (see `pricer_ref.HAR.rate`). This also handles the horizon-rate wrinkle you raised.
4. **Treat the online scale c_coin as a monitor, not a fix.** Clamp it to [0.6, 1.6]. If its raw value leaves [0.4, 2.5], fire an alarm or kill switch. A value of 0.06 means the inputs are broken, and letting the scale absorb it would also hide the broken correlations. Drop the HYPE 1.13 factor.
5. **Remove the shrink.** Keep the t(ν=5) mixture. It is slightly better than a normal distribution from 1 to 7 minutes out, and about equal elsewhere. The cap costs 0.09 nats in the last minute, and wiring up `BaseRateTracker` would not change that.
6. **Correlation:** use a causal EWMA of 60 s returns (sampled every 10 s, 4 h half-life) on the mid; see `corr_series`. Don't hard-code it. Setting the correlation to zero costs 0.11 nats.
7. **Fast path:** precompute W once a second from the current horizon's covariance, then scale by \(\sqrt{\tau_{\text{eff}}}\) ratios within the second. Compute bump deltas on the same draws, and enforce the theo-age kill switch.
8. **Test against `golden_cases.json`.** It holds 4 ticks (840/275/120/30 s left) built from `golden_mids_20261001_1000-1200utc.csv`. Each has log features, rates, sd, R and fair values (200k t-mixture draws; tolerance ±0.005). Your features and rates should match to about 1e-6.

## Files

- `rv2.py`: robust RV features.
- `pricer_ref.py`: reference HAR, τ_eff, correlation EWMA and CRN Monte Carlo pricer.
- `har_coefs.json`: production HAR fit on all 22 days, (k = 10 s, clip 6).
- `diagnose_bars.py`: feed health check for your persisted bars.
- `golden_cases.json`, `golden_mids_*.csv`: regression vectors.
- `comp.py`: mid, microprice and an approximate single-venue CF Spot Rate.
- Validation scripts: `replicate.py` (settlement match), `validate.py`, `analyze.py` and `pooled.py` (Kalshi comparison), `calib.py`, `inject.py`, `har_fit.py`. `har_fit.py` and `calib.py` import `rvlib` from the earlier `rv_model/` package.

## Caveats

- Validation covers two days (151 events). The edge over the market isn't statistically significant; what the data supports is that the theo is calibrated and competitive.
- The market beats the theo in the last minute on Oct 1 (19 snapshots), but not on Sep 1.
- The tick rebuild approximates HAR features from 1-minute candles because the live recorder started after that tick.
