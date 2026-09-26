import json, os, numpy as np, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__))
def load(path=os.path.join(HERE, 'data', 'ticks.jsonl')):  # compact run-1 tick log (not committed)
    L=[json.loads(l) for l in open(path)]
    L=[x for x in L if x['st'].get('mark')]
    n=len(L); D={}
    D['ts']=np.array([x['ts'] for x in L],dtype=np.int64)
    D['mark']=np.array([x['st']['mark'] for x in L],float)
    D['fund']=np.array([x['st'].get('funding_rate') or 0 for x in L],float)
    for k in ('ret_1m','ret_5m','ret_1h','vol_short','range_pos'):
        D[k]=np.array([x['st'].get(k) if x['st'].get(k) is not None else np.nan for x in L],float)
    D['act']=np.array([x['act'] or 'HOLD' for x in L])
    for s in ('BUY','SELL','HOLD'):
        D['p_'+s]=np.array([(x['ap'] or {}).get(s,0) or 0 for x in L],float)
    D['score']=np.array([x['sc'] or 0 for x in L],float)
    D['reentry']=np.array([x['re'] or 0 for x in L],float)
    D['cage']=np.array([x['cg'] or 0 for x in L],float)
    D['aconf']=np.array([x['ac'] or 0 for x in L],float)
    D['logside']=np.array([x['acct'].get('side') or 'flat' for x in L])
    D['possent']=np.array([(x['pos'] or {}).get('side') or 'flat' for x in L])  # position Jev saw (post-fill of that tick)
    D['wa']=np.array([x['wa'].get('action') or 'HOLD' for x in L])
    D['realized']=np.array([x['acct'].get('realized') or 0 for x in L],float)
    D['fees']=np.array([x['acct'].get('fees_paid') or 0 for x in L],float)
    D['fills']=np.array([x['acct'].get('fills') or 0 for x in L],int)
    D['equity']=np.array([x['acct'].get('equity') or 10000 for x in L],float)
    for k in ('book_imb','taker_buy_ratio','ls_ratio','spread_bps','oi'):
        D[k]=np.array([(x['ex'] or {}).get(k) if (x['ex'] or {}).get(k) is not None else np.nan for x in L],float)
    return D

def prep(D):
    import numpy as np
    seen = np.r_[['flat'], D['logside'][:-1]]
    D['genuine'] = (seen == 'flat')
    src = np.full(len(seen), -1); last = -1
    for i, g in enumerate(D['genuine']):
        if g: last = i
        src[i] = last
    D['src'] = src
    return D
