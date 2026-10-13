"""
Degrade Databento-fidelity SPXW quotes into what Interactive Brokers would
actually have delivered, so a backtest measures the edge on the feed we will
trade on rather than on a feed we won't.

Databento's cmbp-1 is every consolidated NBBO change, stamped at the
exchange. IB's `reqMktData` is neither: it delivers "aggregated snapshots
taken at intra-second intervals", 100ms for US options and 250ms for stocks
and futures (interactivebrokers.github.io/tws-api/top_data.html, read
2026-08-28). Two consequences, both modelled below:

  * changes inside a 100ms window collapse to whichever state ended it --
    a quote that moves and reverts within the window never happened as far
    as the strategy is concerned;
  * a change is not knowable until the snapshot that carries it, so the
    earliest the strategy can act on a quote is the END of its window.

What is NOT modelled here is transport: IB gives no exchange timestamp
(`reqTickByTickData`'s is 1-second resolution, too coarse to use), so a real
run also carries an unmeasured exchange->IB->Gateway->socket delay on top,
with jitter. `transport_ms` exists to sweep that sensitivity; it defaults to
0 because this project has no measurement of it yet, and a guessed constant
would read as calibrated when it isn't.
"""
import pandas as pd

# US options bucket. SPXW is an OPRA-disseminated index option, so 100ms --
# not the 250ms futures bucket the old ES-daily path would have sat in.
IB_OPTIONS_CONFLATION_MS = 100


def _conflate_chunk(quotes: pd.DataFrame, interval: pd.Timedelta,
                     transport_ms: float) -> pd.DataFrame:
    df = quotes.sort_values(["raw_symbol", "time"], kind="mergesort")
    slot = df["time"].dt.floor(interval)
    sym = df["raw_symbol"]

    # Last row of each (symbol, window) run. Done with shifts rather than a
    # groupby: these frames run to tens of millions of rows and a
    # groupby(...).tail(1) over that many groups is the expensive way to ask
    # a question the sort has already answered.
    is_last = (sym != sym.shift(-1)) | (slot != slot.shift(-1))
    out = df[is_last].copy()
    out["time"] = slot[is_last] + interval + pd.Timedelta(milliseconds=transport_ms)

    # IB publishes on change, so a window whose state matches the one before
    # it is not a message the strategy would ever have seen.
    prev_sym = out["raw_symbol"].shift()
    unchanged = (out["raw_symbol"] == prev_sym)
    for col in ("bid", "ask", "bid_size", "ask_size"):
        if col in out.columns:
            unchanged &= out[col].eq(out[col].shift())
    return out[~unchanged]


def conflate_to_ib(quotes: pd.DataFrame, interval_ms: int = IB_OPTIONS_CONFLATION_MS,
                    transport_ms: float = 0.0, symbol_chunk_size: int = 20) -> pd.DataFrame:
    """Long-form quotes (see databento_functions.load_option_quotes_long_df)
    resampled to IB's snapshot cadence, in the same shape, so it drops into
    load_event's `spxw_quotes` unchanged.

    Each (contract, window) collapses to the last quote in that window,
    re-stamped to the window's END -- the first moment a snapshot could
    carry it. Windows in which nothing changed emit no row, matching IB's
    change-driven publishing rather than a fixed 10/s heartbeat per
    contract.

    interval_ms=0 returns `quotes` untouched, which is the Databento-fidelity
    baseline the IB numbers are meant to be read against.

    Processed in per-symbol chunks for the same reason build_mids is, and
    documented at length in _raw_to_long_df: sorting the WHOLE frame
    materialises a second copy of it right where it is already largest. On
    KXINXU-26AUG28H1600 (175.9M ticks) that single sort asked for a 3.93 GiB
    contiguous block and OOM-killed the run. Chunking is exact rather than an
    approximation -- conflation never looks across contracts -- and the
    output is ~10x smaller than the input, so only the per-chunk sort is
    ever large.
    """
    if interval_ms <= 0 or quotes.empty:
        return quotes

    interval = pd.Timedelta(milliseconds=interval_ms)
    symbols = pd.unique(quotes["raw_symbol"])
    chunks = []
    for i in range(0, len(symbols), symbol_chunk_size):
        batch = symbols[i:i + symbol_chunk_size]
        sub = quotes[quotes["raw_symbol"].isin(batch)]
        if not sub.empty:
            chunks.append(_conflate_chunk(sub, interval, transport_ms))
    if not chunks:
        return quotes.iloc[0:0]
    return pd.concat(chunks, ignore_index=True).sort_values(
        "time", kind="mergesort").reset_index(drop=True)


def conflation_stats(quotes: pd.DataFrame, interval_ms: int = IB_OPTIONS_CONFLATION_MS) -> dict:
    """How much of the raw feed IB's cadence throws away, for reporting
    alongside a backtest result."""
    n_raw = len(quotes)
    n_ib = len(conflate_to_ib(quotes, interval_ms=interval_ms))
    return {
        "raw_updates": n_raw,
        "ib_updates": n_ib,
        "dropped": n_raw - n_ib,
        "retained_pct": 100.0 * n_ib / n_raw if n_raw else float("nan"),
        "interval_ms": interval_ms,
    }
