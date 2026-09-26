import sys, itertools, json, numpy as np, pandas as pd
from multiprocessing import Pool
import os; HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from load import load, prep
from sim import run, default_params
D = prep(load())
ts = D['ts']; t_split = ts[0] + 0.6*(ts[-1]-ts[0])
SPLIT = int(np.searchsorted(ts, t_split))
exits = [dict(tp=tp, sl=sl) for tp in [6,8,10,12,15,20,30] for sl in [4,6,8,10,15,20]]
exits += [dict(tp=1e6, sl=sl, trail_act=a, trail_dist=d) for (a,d) in [(4,2),(6,3),(8,4),(12,5)] for sl in [6,10,15]]
exits += [dict(tp=tp, sl=sl, trail_act=a, trail_dist=d) for (a,d) in [(4,2),(6,3)] for tp in [10,15] for sl in [6,10]]
holds = [None, 120, 300]
execs = [dict(entry_exec='taker', tp_exec='taker'), dict(entry_exec='taker', tp_exec='maker'),
         dict(entry_exec='maker', tp_exec='taker', maker_wait_s=15), dict(entry_exec='maker', tp_exec='taker', maker_wait_s=60),
         dict(entry_exec='maker', tp_exec='maker', maker_wait_s=15), dict(entry_exec='maker', tp_exec='maker', maker_wait_s=60)]
sides = ['both', 'long', 'short']
stickies = [0, 30, 120, 600]
filters = [dict(score_thr=s, regime=r) for s in [0.35, 0.6] for r in [None, ('no_chase', 0.2)]]
CONFIGS = [{**e, 'max_hold_s': h, **x, 'side': s, 'sticky_s': st, **f}
           for e, h, x, s, st, f in itertools.product(exits, holds, execs, sides, stickies, filters)]
# side blocks: lean/prob filters, vol filter, fade regime, funding tilt (plain holds/stickies subset)
extra = [dict(lean_min=0.2), dict(vol_k=1.0), dict(vol_k=2.0), dict(regime=('fade_only', 0.25)), dict(fund_tilt=True), dict(score_thr=0.75)]
CONFIGS += [{**e, **x, 'side': s, 'sticky_s': st, **ex}
            for e, x, s, st, ex in itertools.product(exits, execs, sides, [0, 120], extra)]

def work(c):
    p = default_params(**c)
    a = run(D, p, 0, SPLIT); b = run(D, p, SPLIT, None)
    row = {'cfg': json.dumps(c)}
    for pre, r in (('is_', a), ('oos_', b)):
        for k, v in r.items():
            row[pre+k] = float(v) if v is not None else np.nan
        row[pre+'fills_day'] = r['fills']/r['hours']*24
        row[pre+'trades_day'] = r['trades']/r['hours']*24
        row[pre+'vol_day'] = r['volume']/r['hours']*24
    return row

if __name__ == '__main__':
    print('configs', len(CONFIGS), 'split idx', SPLIT, 'split time', t_split, flush=True)
    with Pool(8) as pool:
        rows = pool.map(work, CONFIGS, chunksize=100)
    pd.DataFrame(rows).to_csv(os.path.join(HERE, 'grid_all.csv.gz'), index=False)
    print('done', len(rows), flush=True)
