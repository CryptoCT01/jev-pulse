"""Replay simulator for Jev Pulse. Reuses logged Jev decisions; no model calls."""
import numpy as np
TICK = 0.1          # BTCUSDT price tick; logged spread_bps is constant 0.012 = 1 tick
CLIP = 200.0
START = 10_000.0
TAKER = 3e-4        # 0.06% taker less 50% rebate
MAKER = 1e-4        # 0.02% maker less 50% rebate

def default_params(**kw):
    p = dict(sig='logged', score_thr=0.35, cage_thr=0.85, side='both', tp=6.0, sl=6.0,
             trail_act=None, trail_dist=None, max_hold_s=None,
             entry_exec='taker', maker_wait_s=30, tp_exec='taker',
             vol_k=None, fund_tilt=False, regime=None, prob_min=0.0, lean_min=0.0,
             funding=False, sticky_s=0, maker_offset=1, maker_through=1)
    p.update(kw); return p

def signal(D, i, p):
    """+1 long, -1 short, 0 none.
    Model fields come from a GENUINE decision (tick where Jev saw a flat book).
    sticky_s=0: only the genuine tick itself. sticky_s>0: proxy -- reuse the most
    recent genuine decision if it is at most sticky_s seconds old (Jev re-entered the
    same side 75% of the time, median 7 s after a close)."""
    s = D['src'][i]
    if s < 0:
        return 0
    if s != i and (p['sticky_s'] <= 0 or (D['ts'][i] - D['ts'][s]) > p['sticky_s']*1000):
        return 0
    a = D['act'][s]
    if D['cage'][s] >= p['cage_thr']:
        return 0
    if a not in ('BUY', 'SELL'):
        return 0
    if D['score'][s] < p['score_thr']:
        return 0
    d = 1 if a == 'BUY' else -1
    pb, ps = D['p_BUY'][s], D['p_SELL'][s]
    if (pb if d > 0 else ps) < p['prob_min']:
        return 0
    if d * (pb - ps) < p['lean_min']:
        return 0
    if p['side'] == 'long' and d < 0: return 0
    if p['side'] == 'short' and d > 0: return 0
    if p['vol_k'] is not None:
        # expected move proxy: 20-bar 1m stdev (bps) * k must exceed round-trip cost
        rt = (2*TAKER if p['entry_exec'] == 'taker' else MAKER + (MAKER if p['tp_exec']=='maker' else TAKER)) * 1e4
        if not (D['vol_short'][i] * 1e4 * p['vol_k'] > rt):
            return 0
    if p['fund_tilt']:
        f = D['fund'][i]
        # positive funding: longs pay -> only allow long when lean is strong
        if f > 0 and d > 0 and (pb - ps) < 0.3: return 0
        if f < 0 and d < 0 and (ps - pb) < 0.3: return 0
    if p['regime'] is not None:
        rp, vol, r5 = D['range_pos'][i], D['vol_short'][i], abs(D['ret_5m'][i])
        is_range = (vol < 0.0018) or (r5 < 0.0025)
        mode, edge = p['regime']
        if mode == 'fade_only':
            # in Range, only take entries that fade the edge (long near low / short near high)
            if is_range:
                if d > 0 and not (rp <= edge): return 0
                if d < 0 and not (rp >= 1-edge): return 0
        elif mode == 'no_chase':
            # in Range, block entries that chase the edge (long at top / short at bottom)
            if is_range:
                if d > 0 and rp >= 1-edge: return 0
                if d < 0 and rp <= edge: return 0
        elif mode == 'follow_trend':
            if not is_range:
                if np.sign(D['ret_5m'][i]) != d: return 0
    return d

def run(D, p, lo=0, hi=None, record=False):
    m, ts = D['mark'], D['ts']
    hi = len(m) if hi is None else hi
    realized = 0.0; fees = 0.0; vol = 0.0; fund_paid = 0.0
    side = 0; qty = 0.0; entry = 0.0; t_open = 0; peak = 0.0; open_fee = 0.0
    pend = None  # (dir, limit, t_placed)
    trades = []; eq = np.empty(hi-lo); fills = 0
    last_fund_hr = None
    for k, i in enumerate(range(lo, hi)):
        px = m[i]; t = ts[i]; acted = False
        # funding (optional): every 8h at 00/08/16 UTC, longs pay rate*notional
        if p['funding'] and side != 0:
            hr = (t // 3_600_000)
            if last_fund_hr is not None and hr != last_fund_hr and hr % 8 == 0:
                c = side * D['fund'][i] * qty * px
                realized -= c; fund_paid += c
            last_fund_hr = hr
        elif p['funding']:
            last_fund_hr = t // 3_600_000
        # pending maker entry
        if pend is not None and side == 0:
            d, lim, tp0 = pend
            thr = p['maker_through']*TICK
            if (d > 0 and px <= lim - thr + 1e-9) or (d < 0 and px >= lim + thr - 1e-9):
                side, entry, qty, t_open, peak = d, lim, CLIP/lim, t, 0.0
                open_fee = CLIP*MAKER; realized -= open_fee; fees += open_fee; vol += CLIP; fills += 1
                pend = None; acted = True
            elif (t - tp0) > p['maker_wait_s']*1000:
                pend = None
        # exits
        if side != 0 and not acted:
            move = side*(px/entry - 1)*1e4
            peak = max(peak, move)
            ex_px = None; ex_fee = TAKER; why = ''
            if p['tp_exec'] == 'maker':
                tp_px = entry*(1 + side*p['tp']/1e4)
                thr = p['maker_through']*TICK
                if (side > 0 and px >= tp_px + thr - 1e-9) or (side < 0 and px <= tp_px - thr + 1e-9):
                    ex_px, ex_fee, why = tp_px, MAKER, 'tp'
            elif move >= p['tp'] - 1e-6:
                ex_px, why = px, 'tp'
            if ex_px is None and move <= -p['sl'] + 1e-6:
                ex_px, why = px, 'sl'
            if ex_px is None and p['trail_act'] is not None and peak >= p['trail_act'] and move <= peak - p['trail_dist']:
                ex_px, why = px, 'trail'
            if ex_px is None and p['max_hold_s'] is not None and (t - t_open) >= p['max_hold_s']*1000:
                ex_px, why = px, 'time'
            if ex_px is not None:
                notional = qty*ex_px; f = notional*ex_fee
                gross = side*(ex_px - entry)*qty
                realized += gross - f; fees += f; vol += notional; fills += 1
                trades.append((ts[i], side, entry, ex_px, gross, gross - f - open_fee, why, t - t_open))
                side = 0; qty = 0.0; acted = True
        # entries (no same-tick flip)
        if side == 0 and not acted and pend is None:
            d = signal(D, i, p)
            if d != 0:
                if p['entry_exec'] == 'taker':
                    side, entry, qty, t_open, peak = d, px, CLIP/px, t, 0.0
                    open_fee = CLIP*TAKER; realized -= open_fee; fees += open_fee; vol += CLIP; fills += 1
                else:
                    lim = px - p['maker_offset']*TICK if d > 0 else px + p['maker_offset']*TICK   # offset 1 tick => guaranteed passive (spread = 1 tick)
                    pend = (d, lim, t)
        upnl = side*(px - entry)*qty if side else 0.0
        eq[k] = START + realized + upnl
    return summarize(D, lo, hi, eq, trades, fees, vol, fills, realized, record, fund_paid)

def summarize(D, lo, hi, eq, trades, fees, vol, fills, realized, record, fund_paid):
    ts = D['ts'][lo:hi]
    tr = np.array([(x[5], x[4], x[1]) for x in trades]) if trades else np.zeros((0, 3))
    net = eq[-1] - START
    wins = tr[tr[:, 0] >= 0, 0] if len(tr) else np.array([]); losses = tr[tr[:, 0] < 0, 0] if len(tr) else np.array([])
    peak = np.maximum.accumulate(eq); mdd = float((peak - eq).max())
    # hourly Sharpe: last equity in each clock hour, returns on $10k, annualised sqrt(24*365)
    hrs = ts // 3_600_000
    idx = np.r_[np.nonzero(np.diff(hrs))[0], len(hrs)-1]
    he = np.r_[START, eq[idx]]
    r = np.diff(he) / START
    sharpe = float(r.mean()/r.std()*np.sqrt(24*365)) if r.std() > 0 else 0.0
    out = dict(net=net, gross=float(tr[:, 1].sum()) if len(tr) else 0.0, fees=fees, funding=fund_paid, volume=vol,
               fills=fills, trades=len(tr), win_rate=(len(wins)/len(tr)) if len(tr) else np.nan,
               avg_win=wins.mean() if len(wins) else np.nan, avg_loss=losses.mean() if len(losses) else np.nan,
               max_dd=mdd, sharpe_h=sharpe,
               long_n=int((tr[:, 2] > 0).sum()) if len(tr) else 0, short_n=int((tr[:, 2] < 0).sum()) if len(tr) else 0,
               long_pnl=float(tr[tr[:, 2] > 0, 0].sum()) if len(tr) else 0.0, short_pnl=float(tr[tr[:, 2] < 0, 0].sum()) if len(tr) else 0.0,
               hours=(ts[-1]-ts[0])/3.6e6)
    if record:
        out['eq'] = eq; out['trades_list'] = trades
    return out
