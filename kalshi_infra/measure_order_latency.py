"""
Measures REAL take-and-exit order latency against Kalshi's KXINX (S&P 500
range) markets -- the number kalshi_backtest/execution.py's
LatencyModel.send_s should actually be set to, instead of its hardcoded
1.0s placeholder.

!!! THIS PLACES REAL, MARKETABLE ORDERS THAT ARE INTENDED TO FILL !!!

Unlike kalshi_infra/scratch_measure_latency.py (which rests a
deliberately-unmarketable $0.01 order and cancels it, so it never fills and
therefore can't time a real take), this script:

  1. BUYS 1 lot marketably (immediate-or-cancel, priced AT the current ask,
     so it crosses and fills right now with a bounded worst-case price --
     never a naked market order),
  2. times the POST round trip and reads the fill straight off the response,
  3. EXITS by selling that same lot back marketably (IOC AT the current bid),
  4. times that POST round trip too, and confirms the position is flat
     against Kalshi's own portfolio endpoint.

Uses Kalshi's **v2** create-order endpoint (POST
/trade-api/v2/portfolio/events/orders). The v1 endpoint
(/trade-api/v2/portfolio/orders) now returns HTTP 410
`deprecated_v1_order_endpoint` and cannot place orders at all. Beyond
being the only one that works, v2 is a better fit here: it returns
`ts_ms` (the matching-engine timestamp) and `fill_count` on the create
response, so one-way latency is measured directly rather than estimated,
and fills need no position polling.

Cost per trial is the spread crossed twice plus two taker fees -- on the
tightest-spread KXINX market that's typically ~$0.05. The script picks the
tightest two-sided market in the current event specifically to keep that
cost minimal (see pick_market), and refuses to trade a market whose spread
exceeds --max-spread.

What "how long for the exchange to receive our order" actually means here
--------------------------------------------------------------------------
Three different quantities, reported separately because they are NOT
interchangeable and the backtest needs a specific one:

  * rtt_ms      -- full round trip: our send -> exchange receives ->
                   exchange processes/matches -> response back to us. This
                   is what a naive `time.perf_counter()` around the POST
                   measures, and it OVERSTATES send_s because it includes
                   the return leg and the exchange's own matching work.
  * one_way_ms  -- our best estimate of send-to-exchange-receipt alone.
                   Taken from Kalshi's own server-side timestamp on the
                   created order when one is available at usable
                   resolution (see _exchange_recv_time); otherwise falls
                   back to rtt/2, which assumes a symmetric network path
                   and zero exchange-side processing -- a LOWER bound on
                   the true one-way time, flagged as such in the output.
  * fill_ms     -- send -> we have confirmation the lot actually filled.

kalshi_backtest LatencyModel.send_s is defined (see execution.py) as the
one-directional decision -> exchange-receipt delay, so `one_way_ms` is the
one to feed it -- NOT rtt_ms. Using rtt_ms would roughly double the modeled
latency and make the backtest pessimistic in a way live trading wouldn't
actually be.

Usage:
    python -m kalshi_infra.measure_order_latency                 # dry run: pick market, show book, place NOTHING
    python -m kalshi_infra.measure_order_latency --live          # ACTUALLY TRADE (3 trials)
    python -m kalshi_infra.measure_order_latency --live -n 5
    python -m kalshi_infra.measure_order_latency --live --ticker KXINX-26AUG28H1600-B7662

Writes a JSON summary to data/latency/order_latency_{timestamp}.json so the
backtest can read the measured numbers rather than having them retyped by
hand (see kalshi_backtest/execution.py's measured_send_s).
"""
import argparse
import datetime
import json
import os
import sys
import time
import uuid

import numpy as np
import requests


from kalshi_infra import api_functions as af
from kalshi_infra.api_functions import BASE_URL

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "latency")

# A trial is refused outright if the market's spread is wider than this --
# the whole cost of this measurement is the spread crossed twice, so a wide
# market turns a ~5c diagnostic into a multi-dollar one for no extra
# information (latency doesn't depend on which market we cross).
DEFAULT_MAX_SPREAD = 0.10


# ---------------------------------------------------------------------------
# Book / market selection
# ---------------------------------------------------------------------------

def current_event_ticker(series: str = "KXINX") -> str:
    """The open `series` event closing soonest -- the hourly bucket actually
    trading right now. Same rule as kalshi_latency_arbs/live/run_live.py's
    resolve_live_event_ticker (duplicated rather than imported to keep this
    diagnostic standalone/dependency-free w.r.t. the strategy package)."""
    data = af.kalshi_get("/trade-api/v2/markets",
                         params={"series_ticker": series, "status": "open", "limit": 200})
    markets = data.get("markets", [])
    if not markets:
        raise ValueError(f"no open {series} markets right now")
    events = {}
    for m in markets:
        events.setdefault(m["event_ticker"], m["close_time"])
    return min(events, key=lambda et: events[et])


def top_of_book(ticker: str) -> dict:
    """(yes_bid, yes_ask, yes_bid_size, yes_ask_size) in DOLLARS from
    Kalshi's orderbook endpoint.

    Kalshi's orderbook is quoted as resting BIDS on each side -- `yes_dollars`
    are yes bids, `no_dollars` are no bids -- so there is no directly-quoted
    "yes ask": it's derived as 1 - (best no bid), the same YES-side
    convention kalshi_backtest/book.py uses throughout. Best bid on either
    side is the HIGHEST resting price, hence max() rather than the first
    entry (the endpoint returns levels ascending)."""
    ob = af.kalshi_get(f"/trade-api/v2/markets/{ticker}/orderbook",
                       params={"depth": 1}, auth=True).get("orderbook_fp") or {}
    yes_levels = [(float(p), float(s)) for p, s in (ob.get("yes_dollars") or [])]
    no_levels = [(float(p), float(s)) for p, s in (ob.get("no_dollars") or [])]
    yes_bid, yes_bid_size = max(yes_levels) if yes_levels else (None, None)
    best_no, best_no_size = max(no_levels) if no_levels else (None, None)
    yes_ask = (1.0 - best_no) if best_no is not None else None
    return {"ticker": ticker, "yes_bid": yes_bid, "yes_ask": yes_ask,
            "yes_bid_size": yes_bid_size, "yes_ask_size": best_no_size}


def pick_market(event_ticker: str, min_size: float = 1.0) -> dict:
    """The tightest-spread market in `event_ticker` that is quoted on BOTH
    sides with at least `min_size` available on each.

    Both sides are required because the EXIT leg sells the lot back into
    the yes bid -- a market quoted only on one side (the common case for
    deep KXINX tail buckets, confirmed directly: 23 of 30 markets in a live
    event had an empty yes side) would fill the entry and then leave the
    position stranded with nothing to sell into."""
    data = af.kalshi_get("/trade-api/v2/markets",
                         params={"event_ticker": event_ticker, "status": "open", "limit": 200})
    candidates = []
    for m in data.get("markets", []):
        book = top_of_book(m["ticker"])
        if book["yes_bid"] is None or book["yes_ask"] is None:
            continue
        if book["yes_bid_size"] < min_size or book["yes_ask_size"] < min_size:
            continue
        book["spread"] = book["yes_ask"] - book["yes_bid"]
        candidates.append(book)
    if not candidates:
        raise ValueError(f"no two-sided market with size>={min_size} in {event_ticker}")
    return min(candidates, key=lambda b: b["spread"])


# ---------------------------------------------------------------------------
# Timed REST calls
# ---------------------------------------------------------------------------

def _timed_request(method: str, path: str, body: dict = None) -> tuple[float, float, dict]:
    """Returns (send_epoch, rtt_ms, response_json). `send_epoch` is captured
    immediately before the socket write so it can be differenced against
    Kalshi's own server-side timestamp (see _exchange_recv_time)."""
    headers = af._auth_headers(method, path)
    headers["Content-Type"] = "application/json"
    send_epoch = time.time()
    t0 = time.perf_counter()
    resp = requests.request(method, BASE_URL + path, headers=headers, json=body, timeout=15)
    rtt_ms = (time.perf_counter() - t0) * 1000.0
    resp.raise_for_status()
    return send_epoch, rtt_ms, resp.json()


def _exchange_recv_time(order: dict) -> float | None:
    """Kalshi's own matching-engine timestamp for the order, as a unix epoch
    float -- direct evidence of WHEN THE EXCHANGE ACTUALLY PROCESSED it, as
    opposed to when we got a response back.

    The v2 create-order response carries `ts_ms` ("matching engine timestamp
    at which the order was processed, as Unix epoch milliseconds"), which is
    exactly the quantity LatencyModel.send_s models and at high enough
    resolution to be meaningful. The older ISO-8601 `created_time` is kept
    as a fallback but REJECTED when it has whole-second resolution: a
    +/-500ms quantization error on a quantity expected to be well under
    500ms would be less accurate than the rtt/2 fallback, so it's better to
    fall back explicitly than report a precise-looking number built on a
    coarse clock."""
    v = order.get("ts_ms")
    if isinstance(v, (int, float)) and v > 0:
        return float(v) / 1e3
    for field in ("created_ts", "create_ts"):
        v = order.get(field)
        if isinstance(v, (int, float)) and v > 0:
            if v > 1e17:
                return v / 1e9      # nanoseconds
            if v > 1e11:
                return v / 1e3      # milliseconds
            return float(v)         # seconds
    raw = order.get("created_time")
    if isinstance(raw, str) and raw:
        try:
            dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.microsecond == 0:
            return None  # second-resolution only -- see docstring
        return dt.timestamp()
    return None


def _place(ticker: str, direction: str, price: float, count: int = 1) -> tuple[dict, float, float]:
    """One immediate-or-cancel marketable order on the YES leg, via Kalshi's
    v2 create-order endpoint.

    `direction` is "bid" to BUY yes or "ask" to SELL yes -- the v2 API
    replaced v1's (action, side) pair with a single side field naming which
    side of the YES book the order joins. `price` is in dollars.

    time_in_force="immediate_or_cancel" is deliberate and load-bearing: it
    makes the order either take against resting size right now or be
    cancelled outright, so a trial can never accidentally leave a resting
    order on the book if the touch moves between our snapshot and arrival
    (the v1 version of this script needed a separate polling loop and a
    DELETE to clean that up). `fill_count` in the response then tells us
    immediately whether we took, with no position polling required.

    Returns (response_dict, rtt_ms, one_way_ms_or_None)."""
    body = {
        "ticker": ticker,
        "side": direction,
        "count": f"{count:.2f}",
        "price": f"{price:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": str(uuid.uuid4()),
    }
    send_epoch, rtt_ms, resp = _timed_request("POST", "/trade-api/v2/portfolio/events/orders", body)
    recv = _exchange_recv_time(resp)
    one_way_ms = (recv - send_epoch) * 1000.0 if recv is not None else None
    # A server clock even slightly offset from ours makes this negative or
    # absurdly large; treat anything outside the physically plausible
    # [0, rtt] window as unusable rather than silently reporting it.
    if one_way_ms is not None and not (0.0 <= one_way_ms <= rtt_ms):
        one_way_ms = None
    return resp, rtt_ms, one_way_ms


def _position(ticker: str) -> float:
    """Signed net YES contracts currently held on `ticker` (positive = long
    yes, negative = long no), straight from Kalshi's own portfolio endpoint
    -- the authoritative check that an exit actually flattened."""
    data = af.kalshi_get("/trade-api/v2/portfolio/positions",
                         params={"ticker": ticker}, auth=True)
    for p in data.get("market_positions", []):
        if p.get("ticker") == ticker:
            return float(p.get("position", 0))
    return 0.0


# ---------------------------------------------------------------------------
# The measurement itself
# ---------------------------------------------------------------------------

def run_trial(ticker: str, max_spread: float, trial_num: int) -> dict | None:
    """One buy-then-exit round trip. Returns a dict of measurements, or None
    if the trial was skipped (book moved/too wide) or the entry never
    filled. Never leaves a position open on purpose -- an unexited lot is
    reported loudly (see `exited`) rather than silently ignored."""
    book = top_of_book(ticker)
    if book["yes_bid"] is None or book["yes_ask"] is None:
        print(f"  [trial {trial_num}] skipped -- market no longer two-sided")
        return None
    spread = book["yes_ask"] - book["yes_bid"]
    if spread > max_spread:
        print(f"  [trial {trial_num}] skipped -- spread {spread:.2f} > max {max_spread:.2f}")
        return None

    def _one_way_note(one_way, rtt):
        return (f"  one-way={one_way:.1f}ms (matching-engine ts)" if one_way is not None
                else f"  one-way~{rtt / 2:.1f}ms (rtt/2 fallback)")

    buy_price = book["yes_ask"]
    print(f"  [trial {trial_num}] BUY 1 @ {buy_price:.2f} IOC "
          f"(book {book['yes_bid']:.2f}/{book['yes_ask']:.2f})")
    try:
        resp, buy_rtt, buy_one_way = _place(ticker, "bid", buy_price)
    except requests.exceptions.HTTPError as e:
        print(f"  [trial {trial_num}] BUY failed: {e.response.status_code} {e.response.text[:200]}")
        return None
    filled = float(resp.get("fill_count") or 0)
    print(f"           POST rtt={buy_rtt:.1f}ms" + _one_way_note(buy_one_way, buy_rtt))

    # IOC: it either took immediately or was cancelled outright. Nothing can
    # be left resting, so a zero fill just means the ask moved away between
    # our snapshot and arrival -- discard the trial, no cleanup needed.
    if filled == 0:
        print(f"  [trial {trial_num}] IOC did not take (ask moved) -- trial discarded, nothing resting")
        return None
    avg_buy = float(resp.get("average_fill_price") or buy_price)
    buy_fee = float(resp.get("average_fee_paid") or 0.0)
    print(f"           filled {filled:.0f} @ {avg_buy:.4f} (fee {buy_fee:.4f})")

    # --- exit ---
    exit_book = top_of_book(ticker)
    partial = {"buy_rtt_ms": buy_rtt, "buy_one_way_ms": buy_one_way,
               "buy_price": avg_buy, "exited": False}
    if exit_book["yes_bid"] is None:
        print(f"  [trial {trial_num}] !! yes bid vanished -- cannot exit, POSITION LEFT OPEN")
        return partial
    sell_price = exit_book["yes_bid"]
    print(f"  [trial {trial_num}] SELL 1 @ {sell_price:.2f} IOC (exit)")
    try:
        resp2, sell_rtt, sell_one_way = _place(ticker, "ask", sell_price)
    except requests.exceptions.HTTPError as e:
        print(f"  [trial {trial_num}] SELL failed: {e.response.status_code} "
              f"{e.response.text[:200]} -- POSITION LEFT OPEN")
        return partial
    sold = float(resp2.get("fill_count") or 0)
    print(f"           POST rtt={sell_rtt:.1f}ms" + _one_way_note(sell_one_way, sell_rtt))

    if sold == 0:
        print(f"  [trial {trial_num}] !! exit IOC did not take -- POSITION LEFT OPEN")
        partial.update({"sell_rtt_ms": sell_rtt, "sell_one_way_ms": sell_one_way})
        return partial
    avg_sell = float(resp2.get("average_fill_price") or sell_price)
    sell_fee = float(resp2.get("average_fee_paid") or 0.0)

    # Authoritative flatness check -- `fill_count` says the exit matched,
    # but only the portfolio endpoint confirms nothing is left over (e.g. a
    # partial fill on either leg).
    residual = _position(ticker)
    exited = residual == 0
    print(f"           sold {sold:.0f} @ {avg_sell:.4f} (fee {sell_fee:.4f}) -- "
          + ("flat again" if exited else f"!! RESIDUAL POSITION {residual:+.0f}"))

    return {
        "buy_rtt_ms": buy_rtt, "buy_one_way_ms": buy_one_way,
        "sell_rtt_ms": sell_rtt, "sell_one_way_ms": sell_one_way,
        "exited": exited, "buy_price": avg_buy, "sell_price": avg_sell,
        "fees": buy_fee + sell_fee,
        "round_trip_cost": (avg_buy - avg_sell) + buy_fee + sell_fee,
    }


def summarize(trials: list[dict], ticker: str, event_ticker: str) -> dict:
    """Aggregates trials into the numbers the backtest needs, and writes
    them to data/latency/ as JSON. `send_s_estimate` is deliberately built
    from ONE-WAY times (see this module's docstring on why rtt would be
    wrong for LatencyModel.send_s)."""
    def _arr(key):
        return np.array([t[key] for t in trials if t.get(key) is not None], dtype=float)

    buy_rtt, sell_rtt = _arr("buy_rtt_ms"), _arr("sell_rtt_ms")
    all_rtt = np.concatenate([a for a in (buy_rtt, sell_rtt) if len(a)]) if (len(buy_rtt) or len(sell_rtt)) else np.array([])
    buy_ow, sell_ow = _arr("buy_one_way_ms"), _arr("sell_one_way_ms")
    measured_ow = np.concatenate([a for a in (buy_ow, sell_ow) if len(a)]) if (len(buy_ow) or len(sell_ow)) else np.array([])

    used_exchange_ts = len(measured_ow) > 0
    one_way = measured_ow if used_exchange_ts else all_rtt / 2.0

    out = {
        "measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event_ticker": event_ticker, "ticker": ticker, "n_trials": len(trials),
        "one_way_source": "exchange_timestamp" if used_exchange_ts else "rtt_over_2_lower_bound",
    }
    if len(all_rtt):
        out["rtt_ms"] = {"mean": float(all_rtt.mean()), "median": float(np.median(all_rtt)),
                         "p95": float(np.percentile(all_rtt, 95)),
                         "min": float(all_rtt.min()), "max": float(all_rtt.max())}
    if len(buy_rtt):
        out["buy_rtt_ms_mean"] = float(buy_rtt.mean())
    if len(sell_rtt):
        out["exit_rtt_ms_mean"] = float(sell_rtt.mean())
    if len(one_way):
        out["one_way_ms"] = {"mean": float(one_way.mean()), "median": float(np.median(one_way)),
                             "p95": float(np.percentile(one_way, 95))}
        out["send_s_estimate"] = float(np.median(one_way)) / 1000.0
    costs = _arr("round_trip_cost")
    if len(costs):
        out["total_round_trip_cost_usd"] = float(costs.sum())
    out["all_exited"] = all(t.get("exited") for t in trials)

    os.makedirs(_DATA_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    path = os.path.join(_DATA_DIR, f"order_latency_{stamp}.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    out["_path"] = path
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true",
                    help="ACTUALLY PLACE REAL ORDERS. Without this, dry run only.")
    ap.add_argument("-n", "--trials", type=int, default=3)
    ap.add_argument("--series", default="KXINX")
    ap.add_argument("--ticker", default=None, help="Override market auto-selection")
    ap.add_argument("--event-ticker", default=None)
    ap.add_argument("--max-spread", type=float, default=DEFAULT_MAX_SPREAD)
    args = ap.parse_args()

    event_ticker = args.event_ticker or current_event_ticker(args.series)
    if args.ticker:
        book = top_of_book(args.ticker)
        book["spread"] = ((book["yes_ask"] - book["yes_bid"])
                          if None not in (book["yes_ask"], book["yes_bid"]) else float("nan"))
    else:
        book = pick_market(event_ticker)
    ticker = book["ticker"]

    print(f"event:  {event_ticker}")
    print(f"market: {ticker}")
    print(f"book:   bid={book['yes_bid']:.2f} (sz {book['yes_bid_size']:.0f})  "
          f"ask={book['yes_ask']:.2f} (sz {book['yes_ask_size']:.0f})  spread={book['spread']:.2f}")
    est = (book["spread"] + 0.02) * args.trials
    print(f"estimated total cost for {args.trials} trial(s): ~${est:.2f} "
          f"(spread crossed twice + taker fees, per trial)")

    if not args.live:
        print("\nDRY RUN -- no orders placed. Re-run with --live to actually measure.")
        return

    print(f"\n!!! PLACING {args.trials} REAL BUY+SELL ROUND TRIP(S) ON {ticker} !!!\n")
    trials = []
    for i in range(args.trials):
        t = run_trial(ticker, args.max_spread, i + 1)
        if t:
            trials.append(t)
        time.sleep(1.0)

    if not trials:
        print("\nNo usable trials completed.")
        return

    out = summarize(trials, ticker, event_ticker)
    print("\n=== RESULTS ===")
    if "rtt_ms" in out:
        r = out["rtt_ms"]
        print(f"  order round trip (send -> ack):   median={r['median']:.0f}ms  "
              f"mean={r['mean']:.0f}ms  p95={r['p95']:.0f}ms  min={r['min']:.0f}ms  max={r['max']:.0f}ms")
    if "buy_rtt_ms_mean" in out:
        print(f"    entry (buy)  mean rtt: {out['buy_rtt_ms_mean']:.0f}ms")
    if "exit_rtt_ms_mean" in out:
        print(f"    exit (sell)  mean rtt: {out['exit_rtt_ms_mean']:.0f}ms")
    if "one_way_ms" in out:
        o = out["one_way_ms"]
        note = ("from Kalshi's matching-engine ts_ms" if out["one_way_source"] == "exchange_timestamp"
                else "rtt/2 -- a LOWER BOUND, no usable exchange timestamp available")
        print(f"  one-way (send -> exchange processed): median={o['median']:.0f}ms  ({note})")
        print(f"  => LatencyModel.send_s = {out['send_s_estimate']:.4f}s")
    if "total_round_trip_cost_usd" in out:
        print(f"  total cost across trials: ${out['total_round_trip_cost_usd']:.2f} (spread + fees)")
    print(f"  all positions exited: {out['all_exited']}")
    if not out["all_exited"]:
        print("  !! CHECK YOUR ACCOUNT -- at least one lot was not exited.")
    print(f"\n  saved -> {out['_path']}")


if __name__ == "__main__":
    main()
