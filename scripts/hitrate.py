import pandas as pd, numpy as np, math, json
j=pd.read_csv('data/backtest_join.csv',index_col=0,parse_dates=True)
calib=json.load(open('data/model_calib.json'))
PRED='ENSEMBLE_CORR'
j['rounded']=j[PRED].round(0)          # = 官方预报锚定的整数 bucket
j['actual_bucket']=np.floor(j.OBS)     # 结算口径: [N.0, N+1.0)
j['hit']=(j.rounded==j.actual_bucket)
print("=== 若市场把『模型/官方预报的整数档』当作众数，实际命中率是多少？ ===")
print(f"样本 n={len(j)} (lead 0-1天, {j.index.min().date()}~{j.index.max().date()})")
print(f"  预测档 = 实际档 的命中率: {j.hit.mean():.1%}")
print(f"  预测值 MAE { (j.OBS-j[PRED]).abs().mean():.2f}°C, 残差 sd { (j.OBS-j[PRED]).std():.2f}°C\n")

print("=== 给定预测值取整 N，实际结算档相对 N 的偏移分布 ===")
off=(j.actual_bucket-j.rounded).value_counts().sort_index()
for k,v in off.items():
    print(f"   {k:+.0f} 档 : {v/len(j):6.1%}  ({'★市场定价档' if k==0 else ''})")

print("\n=== 用校准残差sd做理论分布 vs 实测 (假设误差~N(0,sd)) ===")
sd=(j.OBS-j[PRED]).std()
def P(lo,hi,mu=0.0,s=sd):
    F=lambda x:0.5*(1+math.erf((x-mu)/(s*math.sqrt(2))))
    return F(hi)-F(lo)
print(f"  sd={sd:.2f}°C, 预测值恰落在整数上时的理论命中率 = {P(0,1):.1%}")
print(f"  ±1档以外概率 = {1-P(-1,2):.1%}")

print("\n=== 交易含义: 若某档市场价 p，按校准概率 q 下注的边际 ===")
q0=j.hit.mean()
for p in [0.35,0.40,0.45,0.50,0.55,0.60,0.70]:
    ev=1/p*q0-1 if p>0 else 0
    print(f"  市场价 {p:.0%} vs 真实概率 {q0:.0%} -> 买YES每$1期望 {q0/p:.2f}  ({'✅+EV' if ev>0 else '❌-EV'} {ev:+.0%})")
json.dump({'hit_rate':float(j.hit.mean()),'resid_sd':float(sd),'n':int(len(j))},
          open('data/hitrate.json','w'),indent=1)
