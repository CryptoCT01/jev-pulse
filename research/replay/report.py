import sys, json, numpy as np, pandas as pd, datetime as dt
import os; HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
from load import load, prep; from sim import run, default_params
D = prep(load()); ts = D['ts']; SPLIT = int(np.searchsorted(ts, ts[0]+0.6*(ts[-1]-ts[0])))
V = {
 'A current (TP6/SL6 taker)': dict(),
 'B taker entry + maker TP 6 / SL6': dict(entry_exec='taker', tp_exec='maker'),
 'C maker entry(60s)+maker TP 6/SL6, sticky600': dict(entry_exec='maker', tp_exec='maker', maker_wait_s=60, sticky_s=600),
 'D taker, maker TP6 / SL4, funding tilt': dict(tp=6, sl=4, entry_exec='taker', tp_exec='maker', fund_tilt=True),
 'E maker entry(15s), trail 4/2, SL15, 120s time stop, no-chase, sticky600': dict(tp=1e6, sl=15, trail_act=4, trail_dist=2, max_hold_s=120, entry_exec='maker', tp_exec='taker', maker_wait_s=15, sticky_s=600, regime=('no_chase', 0.2)),
 'F taker TP20/SL4, 120s time stop, score>=0.6, sticky30': dict(tp=20, sl=4, max_hold_s=120, score_thr=0.6, sticky_s=30),
 'G maker entry(60s), TP20/SL10, 120s stop, score>=0.6, sticky600': dict(tp=20, sl=10, max_hold_s=120, entry_exec='maker', tp_exec='taker', maker_wait_s=60, sticky_s=600, score_thr=0.6),
 'H current rules, long-only': dict(side='long'),
 'I current rules, short-only': dict(side='short'),
 'J (low-freq side note) taker TP20/SL15': dict(tp=20, sl=15),
 'K (low-freq side note) short-only fade-range, maker entry, TP30/SL20': dict(tp=30, sl=20, entry_exec='maker', maker_wait_s=15, side='short', regime=('fade_only', 0.25)),
}
rows = []; curves = {}
cur = {}
for name, cfg in V.items():
    p = default_params(**cfg)
    full = run(D, p, record=True); a = run(D, p, 0, SPLIT); b = run(D, p, SPLIT, None)
    fundf = run(D, default_params(**cfg, funding=True))
    curves[name] = full['eq']
    for seg, r in (('full', full), ('IS', a), ('OOS', b)):
        rows.append(dict(variant=name, segment=seg, net=r['net'], gross=r['gross'], fees=r['fees'], volume=r['volume'],
                         fills=r['fills'], trades=r['trades'], fills_per_day=r['fills']/r['hours']*24, trades_per_day=r['trades']/r['hours']*24,
                         volume_per_day=r['volume']/r['hours']*24, net_bps_of_volume=r['net']/r['volume']*1e4 if r['volume'] else np.nan,
                         win_rate=r['win_rate'], avg_win=r['avg_win'], avg_loss=r['avg_loss'], max_dd=r['max_dd'], sharpe_hourly_ann=r['sharpe_h'],
                         long_trades=r['long_n'], short_trades=r['short_n'], long_pnl=r['long_pnl'], short_pnl=r['short_pnl'], hours=r['hours'],
                         net_with_funding=fundf['net'] if seg=='full' else np.nan, cfg=json.dumps(cfg)))
df = pd.DataFrame(rows)
base = df[(df.variant.str.startswith('A '))].set_index('segment')
df['fills_pct_of_current'] = df.apply(lambda r: r.fills / base.loc[r.segment, 'fills'] * 100, axis=1)
df['HF_ok(>=70%)'] = df.fills_pct_of_current >= 70
df.to_csv(os.path.join(HERE, 'results_variants.csv'), index=False)
pd.set_option('display.width', 250); pd.set_option('display.max_columns', 40)
show = ['net','gross','fees','volume','trades_per_day','fills_per_day','fills_pct_of_current','net_bps_of_volume','win_rate','avg_win','avg_loss','max_dd','sharpe_hourly_ann','long_pnl','short_pnl']
for seg in ('IS','OOS','full'):
    print('====', seg); print(df[df.segment==seg].set_index('variant')[show].round(2).to_string())
print('funding impact (full):'); print(df[df.segment=='full'].set_index('variant')[['net','net_with_funding']].round(3).to_string())
# chart
f = lambda ms: dt.datetime.fromtimestamp(ms/1000, dt.timezone.utc).replace(tzinfo=None)
x = [f(t) for t in ts]
fig, ax = plt.subplots(2, 1, figsize=(13, 8), sharex=True, gridspec_kw=dict(height_ratios=[3, 1]))
pick = ['A current (TP6/SL6 taker)', 'C maker entry(60s)+maker TP 6/SL6, sticky600', 'D taker, maker TP6 / SL4, funding tilt', 'E maker entry(15s), trail 4/2, SL15, 120s time stop, no-chase, sticky600']
for n in pick:
    ax[0].plot(x, curves[n] - 10000, lw=1.3, label=n)
ax[0].axhline(0, color='k', lw=.6)
for a_ in ax: a_.axvspan(x[SPLIT], x[-1], color='grey', alpha=.15)
ax[0].text(x[SPLIT], ax[0].get_ylim()[1]*0.9 if ax[0].get_ylim()[1] > 0 else 2, '  out-of-sample (last 40%)', fontsize=9)
ax[0].set_ylabel('Paper PnL on $10k, $ (one $200 clip)'); ax[0].legend(fontsize=8, loc='lower left')
ax[0].set_title('Jev Pulse replay, 23-26 Sep 2026 (UTC): current rules vs best high-frequency variants (fills >= 70% of current)')
ax[1].plot(x, D['mark'], color='tab:orange', lw=.8); ax[1].set_ylabel('BTCUSDT last')
plt.tight_layout(); plt.savefig(os.path.join(HERE, 'equity_curves.png'), dpi=120)
print('saved')
