#!/usr/bin/env python3
"""Round 3: Portfolio refinement + 5 new hypotheses."""
import pickle,csv,math,random,time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date,datetime,timedelta
from pathlib import Path
import numpy as np,pytz,warnings
warnings.filterwarnings("ignore")
import requests as req

BASE=Path("/root/spy-0dte-trader"); DATA_DIR=BASE/"backtest_data"
RES =BASE/"hermes_research/results_r3"; RES.mkdir(exist_ok=True)
LOG =BASE/"hermes_research/r3_output.log"
ET  =pytz.timezone("America/New_York")
TMINS=252*390
_RFR={2021:.001,2022:.015,2023:.045,2024:.050,2025:.045,2026:.043}
rfr=lambda d:_RFR.get(d.year,.045)
SPLIT=date(2023,7,1); BLIND=2025

def _env():
    e={}
    p=BASE/".env"
    if p.exists():
        for l in p.read_text().splitlines():
            if "=" in l and not l.startswith("#"):
                k,v=l.split("=",1); e[k.strip()]=v.strip().strip("'\"")
    return e
ENV=_env()
TGT=ENV.get("TELEGRAM_BOT_TOKEN",""); TGC=ENV.get("TELEGRAM_CHAT_ID","")
def tg(m):
    try: req.post("https://api.telegram.org/bot"+TGT+"/sendMessage",json={"chat_id":TGC,"text":m,"parse_mode":"Markdown"},timeout=8)
    except: pass

from py_vollib.black_scholes import black_scholes as pvbs
from py_vollib.black_scholes.greeks.analytical import delta as pvd

def bsp(S,K,T,r,s,f):
    if T<=1e-7 or s<=1e-6: return max(S-K,0) if f=="c" else max(K-S,0)
    try: return float(pvbs(f,S,K,T,r,s))
    except: return .01

def bsd(S,K,T,r,s,f):
    if T<=1e-7 or s<=1e-6: return (1. if S>K else 0.) if f=="c" else (-1. if S<K else 0.)
    try: return float(pvd(f,S,K,T,r,s))
    except: return 0.

def skd(tgt,S,T,r,iv,f,n=60):
    best_k,best_d=S,999.
    lo=S*.85 if f=="p" else S; hi=S if f=="p" else S*1.15
    for i in range(n):
        k=lo+(hi-lo)*i/n; d=abs(bsd(S,k,T,r,iv,f))
        if abs(d-tgt)<best_d: best_d,best_k=abs(d-tgt),k
    return round(best_k/.5)*.5

def opt(S,K,ml,vix,f,sp=.065):
    T=max(ml,.5)/TMINS; r=.045; iv=vix/100
    mid=max(bsp(S,K,T,r,iv,f),.01)
    return mid*(1-sp/2),mid*(1+sp/2)

def vwap(bars):
    tv=sum(b["v"]*(b["h"]+b["l"]+b["c"])/3 for b in bars if b["v"]>0)
    v=sum(b["v"] for b in bars if b["v"]>0)
    return tv/v if v>0 else (bars[-1]["c"] if bars else 0)

def load_ma50(spy_d):
    dates=sorted(spy_d.keys()); closes=[spy_d[d]["close"] for d in dates]
    return {ds:sum(closes[i-49:i+1])/50 if i>=49 else None for i,ds in enumerate(dates)}

print("Loading data...")
with (DATA_DIR/"spy_daily_2021-01-04_2026-05-30.pkl").open("rb") as f: spy_d=pickle.load(f)
with (DATA_DIR/"spy_5min_2021-01-04_2026-05-30.pkl").open("rb") as f: spy_5=pickle.load(f)
vix_d={}
with (DATA_DIR/"vix_2021-01-04_2026-05-30.csv").open() as f:
    for row in csv.DictReader(f): vix_d[row["date"]]=float(row["vix"])
exps=sorted(date.fromisoformat(p.stem[10:]) for p in DATA_DIR.glob("theta_SPY_????-??-??.pkl") if len(p.stem)==len("theta_SPY_2021-01-04"))
exps_set=set(exps)
dlist=sorted(spy_d.keys()); ma50=load_ma50(spy_d)
print("  "+str(len(dlist))+" days, "+str(len(exps))+" exps, VIX "+str(round(min(vix_d.values()),1))+"-"+str(round(max(vix_d.values()),1)))

FOMC={date(2021,1,27),date(2021,3,17),date(2021,4,28),date(2021,6,16),date(2021,7,28),date(2021,9,22),date(2021,11,3),date(2021,12,15),date(2022,2,2),date(2022,3,16),date(2022,5,4),date(2022,6,15),date(2022,7,27),date(2022,9,21),date(2022,11,2),date(2022,12,14),date(2023,2,1),date(2023,3,22),date(2023,5,3),date(2023,6,14),date(2023,7,26),date(2023,9,20),date(2023,11,1),date(2023,12,13),date(2024,1,31),date(2024,3,20),date(2024,5,1),date(2024,6,12),date(2024,7,31),date(2024,9,18),date(2024,11,7),date(2024,12,18),date(2025,1,29),date(2025,3,19),date(2025,5,7),date(2025,6,18),date(2025,7,30),date(2025,9,17),date(2025,11,5),date(2025,12,10),date(2026,1,28),date(2026,3,18),date(2026,5,6)}

@dataclass
class T:
    s:str; date:str; ed:str; xd:str; ep:float; xp:float; pnl:float; vix:float; note:str=""

def stats(trades):
    if not trades: return {"n":0,"wr":0,"total_pnl":0,"pf":0,"sharpe":0,"max_dd":0,"wins":0,"losses":0}
    wins=[t for t in trades if t.pnl>0]; losses=[t for t in trades if t.pnl<=0]
    gw=sum(t.pnl for t in wins); gl=sum(t.pnl for t in losses)
    wr=len(wins)/len(trades)
    eq=peak=mdd=0.
    for t in sorted(trades,key=lambda x:x.ed):
        eq+=t.pnl; peak=max(peak,eq); mdd=max(mdd,peak-eq)
    by_d=defaultdict(float)
    for t in trades: by_d[t.ed]+=t.pnl
    daily=list(by_d.values())
    if len(daily)>1:
        mu,sig=np.mean(daily),np.std(daily,ddof=1)
        sh=(mu/sig*math.sqrt(252)) if sig>0 else 0.
    else: sh=0.
    pf=abs(gw/gl) if gl<0 else float("inf")
    return {"n":len(trades),"wr":wr*100,"total_pnl":sum(t.pnl for t in trades),"pf":pf,"sharpe":sh,"max_dd":mdd,"wins":len(wins),"losses":len(losses)}

def bp(trades,n=1500):
    if not trades: return 0.
    pnls=[t.pnl for t in trades]
    return sum(1 for _ in range(n) if sum(random.choices(pnls,k=len(pnls)))>0)/n*100

def yc(trades):
    if not trades: return 1.
    tot=sum(t.pnl for t in trades)
    if tot<=0: return 1.
    by_y=defaultdict(float)
    for t in trades: by_y[t.ed[:4]]+=t.pnl
    return max(by_y.values())/tot

def kill(oos,bwr=55.):
    s=stats(oos)
    if s["n"]<20: return True,"OOS N="+str(s["n"])+"<20"
    if s["sharpe"]<1.: return True,"OOS Sharpe="+str(round(s["sharpe"],2))+"<1.0"
    if s["wr"]<bwr: return True,"OOS WR="+str(round(s["wr"],1))+"% < "+str(bwr)+"%"
    c=yc(oos)
    if c>.60: return True,"Year conc "+str(round(c*100))+"%"
    return False,"ALIVE"

def cs_entry(ds,entry_h,entry_m0,entry_m1):
    """Returns (entry_bar, dt_entry, spy_e, ml) or None."""
    bars=spy_5.get(ds,[])
    if len(bars)<8: return None
    dt_obj=date.fromisoformat(ds)
    exp=dt_obj if dt_obj in exps_set else None
    if exp is None:
        for e in exps:
            if 0<=(e-dt_obj).days<=1: exp=e; break
    if exp is None: return None
    eb=None
    for bar in bars:
        db=datetime.fromtimestamp(bar["t"]/1000,tz=ET)
        if db.hour==entry_h and entry_m0<=db.minute<entry_m1: eb=bar; break
    if eb is None: return None
    dt_eb=datetime.fromtimestamp(eb["t"]/1000,tz=ET)
    ml=max(16*60-(dt_eb.hour*60+dt_eb.minute),1)
    return eb,dt_eb,eb["c"],ml,bars

def run_cs(ds,flag,delta_tgt,long_width,min_cred_mult,tgt_pct,stop_mult,exit_h,entry_h,entry_m0,entry_m1,name,extra_filter=None):
    res=cs_entry(ds,entry_h,entry_m0,entry_m1)
    if res is None: return None
    eb,dt_eb,spy_e,ml,bars=res
    dt_obj=date.fromisoformat(ds)
    vix=vix_d.get(ds,16.)
    r=rfr(dt_obj); iv=vix/100; T_e=ml/TMINS
    sk=skd(delta_tgt,spy_e,T_e,r,iv,flag,60)
    lk=(sk-long_width) if flag=="p" else (sk+long_width)
    sc_b,_=opt(spy_e,sk,ml,vix,flag); _,lp_a=opt(spy_e,lk,ml,vix,flag)
    credit=round(sc_b-lp_a,4)
    if credit<max(.08,vix*min_cred_mult): return None
    if extra_filter and not extra_filter(ds,bars,eb,dt_eb,spy_e,vix): return None
    tgt_e=credit*tgt_pct; stp_e=credit*stop_mult
    eb_x=None; rsn="EOD"
    for bar in bars:
        db=datetime.fromtimestamp(bar["t"]/1000,tz=ET)
        if db<=dt_eb: continue
        if (db.hour,db.minute)>=(exit_h,0): eb_x=bar; rsn=str(exit_h)+"PM"; break
        ml2=max(16*60-(db.hour*60+db.minute),1)
        sa,_=opt(bar["c"],sk,ml2,vix,flag); _,lb=opt(bar["c"],lk,ml2,vix,flag)
        cur=max(sa-lb,0)
        if cur<=tgt_e: eb_x=bar; rsn="target"; break
        if cur>=stp_e: eb_x=bar; rsn="stop"; break
    if eb_x is None: eb_x=bars[-1]
    db_x=datetime.fromtimestamp(eb_x["t"]/1000,tz=ET)
    ml_x=max(16*60-(db_x.hour*60+db_x.minute),1)
    sa_x,_=opt(eb_x["c"],sk,ml_x,vix,flag); _,lb_x=opt(eb_x["c"],lk,ml_x,vix,flag)
    exit_d=max(sa_x-lb_x,0)
    pnl=round((credit-exit_d)*100,2)
    return T(name,ds,ds,ds,credit,exit_d,pnl,vix,rsn)

# R3A: Monday-only put spread
def run_r3a():
    trades=[]
    for ds in dlist:
        dt=date.fromisoformat(ds)
        if dt.weekday()!=0: continue
        vix=vix_d.get(ds,16.)
        if not (13<=vix<=22): continue
        t=run_cs(ds,"p",.16,2.,.009,.25,1.75,15,10,30,60,"R3A_Mon_Only")
        if t: trades.append(t)
    return trades

# R3B: Wednesday-only put spread
def run_r3b():
    trades=[]
    for ds in dlist:
        dt=date.fromisoformat(ds)
        if dt.weekday()!=2: continue
        vix=vix_d.get(ds,16.)
        if not (13<=vix<=22): continue
        t=run_cs(ds,"p",.16,2.,.009,.25,1.75,15,10,30,60,"R3B_Wed_Only")
        if t: trades.append(t)
    return trades

# R3C: Call credit spread on below-VWAP days
def run_r3c():
    def below_vwap(ds,bars,eb,dt_eb,spy_e,vix):
        bef=[b for b in bars if b["t"]<=eb["t"]]
        vw=vwap(bef) if bef else spy_e
        return spy_e<vw
    trades=[]
    for ds in dlist:
        dt=date.fromisoformat(ds)
        if dt.weekday() not in (0,2,4): continue
        vix=vix_d.get(ds,16.)
        if not (13<=vix<=25): continue
        t=run_cs(ds,"c",.16,2.,.008,.25,1.75,15,10,30,60,"R3C_Call_CS",below_vwap)
        if t: trades.append(t)
    return trades

# R3D: R2 with FOMC-week skip
def run_r3d():
    fomc_weeks=set()
    for fd in FOMC:
        for i in range(-2,3): fomc_weeks.add((fd+timedelta(days=i)).isoformat())
    trades=[]
    for ds in dlist:
        if ds in fomc_weeks: continue
        dt=date.fromisoformat(ds)
        if dt.weekday() not in (0,2,4): continue
        vix=vix_d.get(ds,16.)
        if not (13<=vix<=22): continue
        t=run_cs(ds,"p",.16,2.,.009,.25,1.75,15,10,30,60,"R3D_R2_NoFOMC")
        if t: trades.append(t)
    return trades

# R3E: Iron condor on low-VIX Wednesdays
def run_r3e():
    trades=[]
    for ds in dlist:
        dt=date.fromisoformat(ds)
        if dt.weekday()!=2: continue
        vix=vix_d.get(ds,16.)
        if not (13<=vix<=18): continue
        bars=spy_5.get(ds,[])
        if len(bars)<8: continue
        exp=dt if dt in exps_set else None
        if exp is None:
            for e in exps:
                if 0<=(e-dt).days<=1: exp=e; break
        if exp is None: continue
        eb=None
        for bar in bars:
            db=datetime.fromtimestamp(bar["t"]/1000,tz=ET)
            if db.hour==10 and 30<=db.minute<60: eb=bar; break
        if eb is None: continue
        dt_eb=datetime.fromtimestamp(eb["t"]/1000,tz=ET)
        spy_e=eb["c"]; r=rfr(dt); iv=vix/100
        ml=max(16*60-(dt_eb.hour*60+dt_eb.minute),1); T_e=ml/TMINS
        sk_p=skd(.16,spy_e,T_e,r,iv,"p",60); lk_p=sk_p-2.
        sk_c=skd(.16,spy_e,T_e,r,iv,"c",60); lk_c=sk_c+2.
        sp_b,_=opt(spy_e,sk_p,ml,vix,"p"); _,lp_a=opt(spy_e,lk_p,ml,vix,"p")
        sc_b,_=opt(spy_e,sk_c,ml,vix,"c"); _,lc_a=opt(spy_e,lk_c,ml,vix,"c")
        credit=round((sp_b-lp_a)+(sc_b-lc_a),4)
        if credit<max(.15,vix*.011): continue
        tgt_e=credit*.40; stp_e=credit*1.75
        eb_x=None; rsn="EOD"
        for bar in bars:
            db=datetime.fromtimestamp(bar["t"]/1000,tz=ET)
            if db<=dt_eb: continue
            if (db.hour,db.minute)>=(15,0): eb_x=bar; rsn="3PM"; break
            ml2=max(16*60-(db.hour*60+db.minute),1)
            sp=bar["c"]
            sa_p,_=opt(sp,sk_p,ml2,vix,"p"); _,lb_p=opt(sp,lk_p,ml2,vix,"p")
            sa_c,_=opt(sp,sk_c,ml2,vix,"c"); _,lb_c=opt(sp,lk_c,ml2,vix,"c")
            cur=max((sa_p-lb_p)+(sa_c-lb_c),0)
            if cur<=tgt_e: eb_x=bar; rsn="target"; break
            if cur>=stp_e: eb_x=bar; rsn="stop"; break
        if eb_x is None: eb_x=bars[-1]
        db_x=datetime.fromtimestamp(eb_x["t"]/1000,tz=ET)
        ml_x=max(16*60-(db_x.hour*60+db_x.minute),1)
        sp_x=eb_x["c"]
        sa_px,_=opt(sp_x,sk_p,ml_x,vix,"p"); _,lb_px=opt(sp_x,lk_p,ml_x,vix,"p")
        sa_cx,_=opt(sp_x,sk_c,ml_x,vix,"c"); _,lb_cx=opt(sp_x,lk_c,ml_x,vix,"c")
        exit_d=max((sa_px-lb_px)+(sa_cx-lb_cx),0)
        pnl=round((credit-exit_d)*100,2)
        trades.append(T("R3E_IronCondor_LowVIX",ds,ds,ds,credit,exit_d,pnl,vix,rsn))
    return trades

STRATS=[
    ("R3A","Monday Only Credit Spread","Mon 10:30 AM put spread — exploit 90.6% WR day",run_r3a,55.),
    ("R3B","Wednesday Only Credit Spread","Wed 10:30 AM put spread — 85.2% WR day",run_r3b,55.),
    ("R3C","Bear Call Credit Spread","Sell OTM calls when SPY<VWAP — bear complement to R2",run_r3c,55.),
    ("R3D","R2 minus FOMC Weeks","R2 skipping ±2d around FOMC — remove binary risk",run_r3d,55.),
    ("R3E","Iron Condor Low-VIX Wed","Wed iron condor VIX 13-18 — range-bound capture",run_r3e,55.),
]

results=[]
log=open(str(LOG),"w",buffering=1)
def p(m): print(m); log.write(m+"\n")

p("\n"+"="*60+"\n  HERMES ROUND 3 — "+datetime.now().strftime("%Y-%m-%d %H:%M:%S")+"\n"+"="*60+"\n")
tg("🔬 *Round 3 starting*\nPortfolio refinement of R2 survivors (R2/R8/R10) + 5 new hypotheses")

alive_all=[]
for i,(sid,name,desc,runner,bwr) in enumerate(STRATS):
    p("\n  ["+str(i+1)+"/5] "+sid+": "+name)
    p("  "+desc)
    t0=time.time()
    try: all_t=runner()
    except Exception as e:
        import traceback; traceback.print_exc(); all_t=[]
    elapsed=time.time()-t0
    is_t =[t for t in all_t if t.ed<SPLIT.isoformat()]
    oos_t=[t for t in all_t if SPLIT.isoformat()<=t.ed and not t.ed.startswith(str(BLIND))]
    bld_t=[t for t in all_t if t.ed.startswith(str(BLIND))]
    s_all=stats(all_t); s_oos=stats(oos_t); s_bld=stats(bld_t)
    dead,kill_r=kill(oos_t,bwr)
    by_yr=defaultdict(list)
    for t in all_t: by_yr[t.ed[:4]].append(t)
    p("  Full: N="+str(s_all["n"])+" WR="+str(round(s_all["wr"],1))+"% P&L=$"+str(round(s_all["total_pnl"],2))+" Sharpe="+str(round(s_all["sharpe"],2)))
    p("  OOS:  N="+str(s_oos["n"])+" WR="+str(round(s_oos["wr"],1))+"% P&L=$"+str(round(s_oos["total_pnl"],2))+" Sharpe="+str(round(s_oos["sharpe"],2)))
    p("  Blind "+str(BLIND)+": N="+str(s_bld["n"])+" WR="+str(round(s_bld["wr"],1))+"% P&L=$"+str(round(s_bld["total_pnl"],2)))
    p("  "+("DEAD: "+kill_r if dead else "ALIVE"))
    for yr in sorted(by_yr.keys()):
        ys=stats(by_yr[yr])
        p("    "+yr+": N="+str(ys["n"])+" WR="+str(round(ys["wr"],1))+"% P&L=$"+str(round(ys["total_pnl"],2)))
    er=defaultdict(lambda:{"n":0,"pnl":0.,"w":0})
    for t in all_t:
        er[t.note]["n"]+=1; er[t.note]["pnl"]+=t.pnl
        if t.pnl>0: er[t.note]["w"]+=1
    p("  Exit reasons:")
    for rsn,v in sorted(er.items(),key=lambda x:-x[1]["n"]):
        wr2=v["w"]/v["n"]*100 if v["n"] else 0
        p("    "+rsn+": N="+str(v["n"])+" WR="+str(round(wr2,1))+"% P&L=$"+str(round(v["pnl"],2)))
    r={"id":sid,"name":name,"dead":dead,"kill_r":kill_r,"s_oos":s_oos,"s_bld":s_bld,"s_all":s_all}
    results.append(r)
    if not dead: alive_all.append({**r,"trades":all_t})

p("\n"+"="*60)
p("  ROUND 3: "+str(len(alive_all))+"/5 survived")
p("="*60)

# Portfolio stats
p("\n  Computing R2 base portfolio...")
r2_trades=[]
for ds in dlist:
    dt=date.fromisoformat(ds)
    if dt.weekday() not in (0,2,4): continue
    vix=vix_d.get(ds,16.)
    if not (13<=vix<=22): continue
    t=run_cs(ds,"p",.16,2.,.009,.25,1.75,15,10,30,60,"R2")
    if t: r2_trades.append(t)

combined=r2_trades[:]
for r in alive_all: combined+=r["trades"]
combined_oos=[t for t in combined if SPLIT.isoformat()<=t.ed and not t.ed.startswith(str(BLIND))]
combined_bld=[t for t in combined if t.ed.startswith(str(BLIND))]
sc=stats(combined); sc_oos=stats(combined_oos); sc_bld=stats(combined_bld)
p("\n  COMBINED PORTFOLIO (R2 + all alive R3):")
p("  Full: N="+str(sc["n"])+" WR="+str(round(sc["wr"],1))+"% P&L=$"+str(round(sc["total_pnl"],2))+" Sharpe="+str(round(sc["sharpe"],2)))
p("  OOS:  N="+str(sc_oos["n"])+" WR="+str(round(sc_oos["wr"],1))+"% P&L=$"+str(round(sc_oos["total_pnl"],2))+" Sharpe="+str(round(sc_oos["sharpe"],2)))
p("  Blind "+str(BLIND)+": N="+str(sc_bld["n"])+" WR="+str(round(sc_bld["wr"],1))+"% P&L=$"+str(round(sc_bld["total_pnl"],2)))

msg="🏁 *Round 3 Complete*\n"+str(len(alive_all))+"/5 new survived\n\n"
for r in alive_all:
    s=r["s_oos"]; sb=r["s_bld"]
    msg+="✅ *"+r["id"]+" "+r["name"]+"*\nOOS: N="+str(s["n"])+" WR="+str(round(s["wr"],1))+"% P&L=$"+str(round(s["total_pnl"],0))+" Sh="+str(round(s["sharpe"],2))+"\nBlind 2025: N="+str(sb["n"])+" WR="+str(round(sb["wr"],1))+"% P&L=$"+str(round(sb["total_pnl"],0))+"\n\n"
msg+="*Portfolio (R2+all survivors):*\nOOS: N="+str(sc_oos["n"])+" WR="+str(round(sc_oos["wr"],1))+"% P&L=$"+str(round(sc_oos["total_pnl"],0))+" Sh="+str(round(sc_oos["sharpe"],2))+"\nBlind 2025: N="+str(sc_bld["n"])+" WR="+str(round(sc_bld["wr"],1))+"% P&L=$"+str(round(sc_bld["total_pnl"],0))
tg(msg)
log.close()
print("Done.")
