"""
Live SPXW option feed over Interactive Brokers TWS/Gateway.

ib_async dispatches ticks on the same asyncio loop as drain() and the
pricing loop, so there is no thread hand-off. See docs/ib-migration.md.
"""
import asyncio
import collections
import dataclasses
import time
import traceback

import numpy as np

from kalshi_infra import ib_functions as ibf
from kalshi_infra import keys

_RATE_WINDOW_S = 10.0     # tick-rate averaging window for records_per_s() (display only)


@dataclasses.dataclass(frozen=True)
class HoldParams:
    """Liquidity-pull hold, the live counterpart of the backtest's
    grid_pricing.hold_through_wide_spread. Values come from config."""
    baseline_s: float
    multiplier: float
    settle_s: float
    min_samples: int
    sample_s: float


class _SpreadHold:
    """Per-contract hold state. The baseline is a trailing median of the
    spread sampled at most every sample_s, so it means the same thing as the
    backtest's (a median over grid points, not over every raw tick, which
    would weight busy moments more)."""
    __slots__ = ("samples", "last_sample_t", "held_bid", "held_ask", "calm_since")

    def __init__(self):
        self.samples: collections.deque = collections.deque()
        self.last_sample_t = -1e18
        self.held_bid = np.nan
        self.held_ask = np.nan
        self.calm_since: float | None = None

    def baseline(self, now: float, hp: HoldParams) -> float:
        cutoff = now - hp.baseline_s
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        if len(self.samples) < hp.min_samples:
            return np.nan
        return float(np.median([s for _, s in self.samples]))


class LiveOptionBook:
    """Parallel np.ndarrays indexed by strike position. call_strikes and
    put_strikes are fixed once the feed starts; snapshot_arrays returns
    copies so the signal loop can hold them across a multi-ms spline fit
    without racing an incoming tick.

    Stores the HELD bid/ask (see _SpreadHold): while a contract's spread is
    abnormally wide, or hasn't been continuously normal for hold.settle_s,
    it keeps reporting its last normal quote. A one-sided quote is a pull
    too and is held the same way."""

    def __init__(self, call_strikes: np.ndarray, put_strikes: np.ndarray, hold: HoldParams):
        self.hold = hold
        self.call_strikes = np.asarray(call_strikes, dtype=float)
        self.put_strikes = np.asarray(put_strikes, dtype=float)
        n_c, n_p = len(self.call_strikes), len(self.put_strikes)
        self._call_bid = np.full(n_c, np.nan)
        self._call_ask = np.full(n_c, np.nan)
        self._put_bid = np.full(n_p, np.nan)
        self._put_ask = np.full(n_p, np.nan)
        self._call_idx = {k: i for i, k in enumerate(self.call_strikes)}
        self._put_idx = {k: i for i, k in enumerate(self.put_strikes)}
        self._hold = {("C", k): _SpreadHold() for k in self.call_strikes}
        self._hold.update({("P", k): _SpreadHold() for k in self.put_strikes})
        self.n_held = 0       # ticks reported as the held quote, for diagnostics

    def set_quote(self, strike: float, right: str, bid: float, ask: float,
                  now: float | None = None) -> bool:
        """Apply one tick. Returns True iff the REPORTED quote moved. IB uses
        -1 and NaN interchangeably for "no quote yet"; both land as NaN."""
        strike = float(strike)
        if right == "C":
            idx = self._call_idx.get(strike)
            if idx is None:
                return False
            bids, asks = self._call_bid, self._call_ask
        else:
            idx = self._put_idx.get(strike)
            if idx is None:
                return False
            bids, asks = self._put_bid, self._put_ask
        now = time.monotonic() if now is None else now
        raw_bid = float(bid) if (bid is not None and bid > 0) else np.nan
        raw_ask = float(ask) if (ask is not None and ask > 0) else np.nan
        new_bid, new_ask = self._apply_hold(self._hold[(right, strike)], raw_bid, raw_ask, now)
        changed = not (_eq_nan(bids[idx], new_bid) and _eq_nan(asks[idx], new_ask))
        bids[idx] = new_bid
        asks[idx] = new_ask
        return changed

    def _apply_hold(self, h: _SpreadHold, bid: float, ask: float, now: float) -> tuple[float, float]:
        two_sided = not (np.isnan(bid) or np.isnan(ask))
        spread = ask - bid if two_sided else np.nan
        hp = self.hold
        base = h.baseline(now, hp)
        # Baseline excludes the current tick, like the backtest's shift(1).
        if two_sided and now - h.last_sample_t >= hp.sample_s:
            h.samples.append((now, spread))
            h.last_sample_t = now
        if np.isnan(base):
            # Not enough history to judge -- pass through (backtest does the same).
            h.held_bid, h.held_ask, h.calm_since = bid, ask, now
            return bid, ask
        anomalous = (not two_sided) or spread > hp.multiplier * base
        if anomalous:
            h.calm_since = None
        elif h.calm_since is None:
            h.calm_since = now
        settled = h.calm_since is not None and now - h.calm_since >= hp.settle_s
        if settled or np.isnan(h.held_bid):
            h.held_bid, h.held_ask = bid, ask
            return bid, ask
        self.n_held += 1
        return h.held_bid, h.held_ask

    def snapshot_arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray,
                                       np.ndarray, np.ndarray, np.ndarray]:
        """(call_mid, call_bid, call_ask, put_mid, put_bid, put_ask).
        Mid is derived, not stored, so a one-sided quote correctly
        produces NaN rather than a stale half-mid."""
        cb, ca = self._call_bid.copy(), self._call_ask.copy()
        pb, pa = self._put_bid.copy(), self._put_ask.copy()
        return (cb + ca) / 2.0, cb, ca, (pb + pa) / 2.0, pb, pa


def _eq_nan(a: float, b: float) -> bool:
    if np.isnan(a) and np.isnan(b):
        return True
    return a == b


def _check_budget(event_ticker: str, strikes) -> None:
    n_lines = 2 * len(strikes)
    if n_lines > ibf.IB_MARKET_DATA_LINES:
        # Over budget doesn't error at IB; the last subscriptions just starve.
        raise ValueError(f"{event_ticker}: {n_lines} contracts exceeds IB's "
                         f"{ibf.IB_MARKET_DATA_LINES}-line budget; pass fewer `tickers`")


def _rebuild_book(old: "LiveOptionBook | None", contracts, hold: HoldParams) -> "LiveOptionBook":
    """Book over `contracts`' strikes, carrying quotes and hold state over
    from `old` for any strike it already had."""
    book = LiveOptionBook(np.asarray(sorted({float(c.strike) for c in contracts if c.right == "C"})),
                          np.asarray(sorted({float(c.strike) for c in contracts if c.right == "P"})), hold)
    if old is None:
        return book
    for right, new_idx, old_idx, nb, na, ob, oa in (
            ("C", book._call_idx, old._call_idx, book._call_bid, book._call_ask, old._call_bid, old._call_ask),
            ("P", book._put_idx, old._put_idx, book._put_bid, book._put_ask, old._put_bid, old._put_ask)):
        for k, i in new_idx.items():
            j = old_idx.get(k)
            if j is not None:
                nb[i], na[i] = ob[j], oa[j]
                book._hold[(right, k)] = old._hold[(right, k)]
    book.n_held = old.n_held
    return book


class LiveIBFeed:
    """One IB socket, subscribed to every SPXW call+put bracketing this
    event's bucket boundaries. `tickers` optionally scopes to the ATM
    subset (same convention as the backtest's scoped SPXW cache)."""

    def __init__(self, event_ticker: str, tickers: list[str] | None, *, client_id: int,
                 hold: HoldParams, reconnect_delay_s: float):
        self.event_ticker = event_ticker
        self.hold = hold
        self.reconnect_delay_s = reconnect_delay_s
        self._tickers_arg = tickers
        self._client_id = client_id

        resolved = ibf.resolve_event_strikes(event_ticker, tickers=tickers)
        self.markets = resolved["markets"]
        self.expiry = resolved["expiry"]
        self.scope_tickers = list(tickers) if tickers is not None else None
        self._initial_strikes = np.asarray(resolved["strikes"], dtype=float)
        _check_budget(event_ticker, self._initial_strikes)
        # Rebuilt in _connect_and_subscribe once we know which strikes IB
        # actually qualified -- a $5 neighbor past the chain's edge may not
        # exist, and the sides can end up with different strike sets.
        self.book = LiveOptionBook(self._initial_strikes.copy(), self._initial_strikes.copy(), hold)

        self._ib_mod = ibf._import_ib()
        ibf.quiet_ib_logging()
        self._ib = self._ib_mod.IB()
        self._conid_to_slot: dict[int, tuple[float, str]] = {}
        # Hold Ticker refs: ib_async weak-references them from the client.
        # Keyed by conId so rescope() can cancel individual lines.
        self._subs: dict[int, tuple] = {}     # conId -> (contract, ticker)
        self._rescope_lock = asyncio.Lock()

        self._tick_times: collections.deque = collections.deque(maxlen=4096)
        self._last_tick_t = time.monotonic()
        self._last_apply_t = time.monotonic()
        self._pending: list[tuple[float, str, float, float, float]] = []
        self.on_tick_hook = None    # called after each queued tick (the runtime's reprice wakeup)

        self._connected = False
        self._stop = False

    async def run(self) -> None:
        """Connect, subscribe, and keep the IB client alive until cancelled.
        Reconnects on dropped socket; staleness guard in LiveEngine catches
        the resulting dead feed (same behavior as any other feed outage)."""
        while not self._stop:
            try:
                await self._connect_and_subscribe()
                while self._ib.isConnected() and not self._stop:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                print(f"[ib_feed] WARN: IB session dropped; reconnecting in {self.reconnect_delay_s}s:")
                traceback.print_exc()
            finally:
                try:
                    if self._ib.isConnected():
                        self._ib.disconnect()
                except Exception:
                    pass
                self._connected = False
                self._conid_to_slot.clear()
                self._subs.clear()
            if self._stop:
                break
            await asyncio.sleep(self.reconnect_delay_s)

    async def _connect_and_subscribe(self) -> None:
        print(f"[ib_feed] connecting to IB at {keys.IB_HOST}:{keys.IB_PORT} "
              f"(clientId={self._client_id})")
        await self._ib.connectAsync(
            host=keys.IB_HOST, port=keys.IB_PORT, clientId=self._client_id,
            account=keys.IB_ACCOUNT or "", readonly=keys.IB_READONLY,
        )
        self._connected = True

        qualified = await self._qualify(self._initial_strikes)
        if not qualified:
            raise RuntimeError(
                f"IB qualified 0/{2 * len(self._initial_strikes)} SPXW contracts for expiry "
                f"{self.expiry}. The 0DTE chain may not be listed yet, or "
                f"the account may lack the OPRA subscription."
            )
        print(f"[ib_feed] IB qualified {len(qualified)}/{2 * len(self._initial_strikes)} "
              f"SPXW contracts for {self.expiry}; subscribing...")
        self.book = _rebuild_book(None, qualified, self.hold)
        self._subs.clear()
        self._conid_to_slot.clear()
        for c in qualified:
            self._subscribe(c)
        print(f"[ib_feed] subscribed to {len(self._subs)} contracts "
              f"({len(self.book.call_strikes)} call strikes, {len(self.book.put_strikes)} put strikes)")

    async def _qualify(self, strikes) -> list:
        contracts = [ibf.make_spxw_option(self.expiry, k, r) for k in strikes for r in ("C", "P")]
        qualified = await self._ib.qualifyContractsAsync(*contracts)
        return [c for c in qualified if c is not None and getattr(c, "conId", 0)]

    def _subscribe(self, c) -> None:
        # Plain streaming top-of-book: no generic ticks, no one-shot
        # snapshots (snapshots are billed from a separate pool).
        t = self._ib.reqMktData(c, genericTickList="", snapshot=False, regulatorySnapshot=False)
        t.updateEvent += self._on_tick
        self._subs[c.conId] = (c, t)
        self._conid_to_slot[c.conId] = (float(c.strike), c.right)

    async def rescope(self, tickers: list[str]) -> bool:
        """Move the subscription to bracket `tickers` without reconnecting.

        New lines are subscribed BEFORE old ones are cancelled, and quotes
        (and hold state) for strikes present in both are carried over, so
        pricing never sees a gap. Returns True if anything changed."""
        async with self._rescope_lock:
            if not self._connected:
                self.scope_tickers = list(tickers)
                self._initial_strikes = np.asarray(
                    ibf.resolve_event_strikes(self.event_ticker, tickers=tickers)["strikes"], dtype=float)
                return True
            want = np.asarray(ibf.resolve_event_strikes(self.event_ticker, tickers=tickers)["strikes"],
                              dtype=float)
            have = {s for s, _ in self._conid_to_slot.values()}
            add = [k for k in want if k not in have]
            drop_strikes = have - set(want)
            if not add and not drop_strikes:
                self.scope_tickers = list(tickers)
                return False
            _check_budget(self.event_ticker, want)
            new_contracts = await self._qualify(add) if add else []
            kept = [c for c, _ in self._subs.values() if float(c.strike) not in drop_strikes]
            for c in new_contracts:
                self._subscribe(c)
            self.book = _rebuild_book(self.book, kept + new_contracts, self.hold)
            for cid in [cid for cid, (c, _) in self._subs.items() if float(c.strike) in drop_strikes]:
                c, _ = self._subs.pop(cid)
                self._conid_to_slot.pop(cid, None)
                try:
                    self._ib.cancelMktData(c)
                except Exception:
                    pass
            self.scope_tickers = list(tickers)
            self._initial_strikes = want
            print(f"[ib_feed] rescoped: +{len(add)} -{len(drop_strikes)} strikes -> "
                  f"{len(self._subs)} contracts ({self.book.call_strikes.min():.0f}-"
                  f"{self.book.call_strikes.max():.0f})")
            return True

    def _on_tick(self, ticker) -> None:
        """IB fires on bid/ask/size/greek updates. Runs on the asyncio loop
        (same thread as drain), so no locking: just append and let the
        signal loop coalesce."""
        slot = self._conid_to_slot.get(getattr(ticker.contract, "conId", 0))
        if slot is None:
            return
        strike, right = slot
        now = time.monotonic()
        # Arrival time travels with the tick so the hold's settle window is
        # measured on the feed's clock, not on when drain() got to it.
        self._pending.append((strike, right, ticker.bid, ticker.ask, now))
        self._tick_times.append(now)
        self._last_tick_t = now
        if self.on_tick_hook is not None:
            self.on_tick_hook()

    def drain(self) -> int:
        """Apply pending ticks to the book. Returns N applied (not N
        changed) -- a repeating same-value tick is still proof the feed
        is live. Clears _pending in place."""
        if not self._pending:
            self._last_apply_t = time.monotonic()
            return 0
        batch = self._pending
        self._pending = []
        for strike, right, bid, ask, t in batch:
            self.book.set_quote(strike, right, bid, ask, now=t)
        self._last_apply_t = time.monotonic()
        return len(batch)

    def records_per_s(self) -> float:
        """Tick arrival rate over the last _RATE_WINDOW_S. Dashboard-only;
        the staleness guard uses seconds_since_record instead."""
        now = time.monotonic()
        cutoff = now - _RATE_WINDOW_S
        while self._tick_times and self._tick_times[0] < cutoff:
            self._tick_times.popleft()
        if not self._tick_times:
            return 0.0
        span = now - self._tick_times[0]
        if span <= 0:
            return 0.0
        return len(self._tick_times) / span

    def seconds_since_record(self) -> float:
        """Age of the last arrived tick; the only clock that catches a
        dead IB socket while the loop itself is fine."""
        return time.monotonic() - self._last_tick_t

    def seconds_since_applied(self) -> float:
        """Age of the last drain(). Growing here with records_per_s()
        healthy means the loop is behind, not that the feed is."""
        return time.monotonic() - self._last_apply_t

    def stop(self) -> None:
        self._stop = True
        try:
            if self._ib.isConnected():
                self._ib.disconnect()
        except Exception:
            pass
