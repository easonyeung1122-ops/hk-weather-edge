import pandas as pd, numpy as np, json, urllib.request, warnings
warnings.filterwarnings('ignore')
LAT,LON=22.302,114.173
MODELS="ecmwf_ifs025,ecmwf_aifs025,gfs_seamless,icon_seamless,meteofrance_seamless,gem_seamless,metno_seamless,jma_seamless,ukmo_seamless"

def get(u): return json.load(urllib.request.urlopen(u,timeout=120))

df=pd.read_csv('data/maxt_HKO.csv',skiprows=2,encoding='utf-8-sig',names=['y','m','d','v','c'])
df['v']=pd.to_numeric(df['v'],errors='coerce'); df=df.dropna(subset=['v'])
df['date']=pd.to_datetime(dict(year=df.y,month=df.m,day=df.d))
obs=df.set_index('date')['v'].sort_index()

u=(f"https://previous-runs-api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
   f"&daily=temperature_2m_max&past_days=92&forecast_days=1&timezone=Asia%2FShanghai&models={MODELS}")
d=get(u)['daily']
fc=pd.DataFrame({k.replace('temperature_2m_max_',''):v for k,v in d.items() if k!='time'},
                index=pd.to_datetime(d['time'])).dropna(axis=1,how='all')
print("可用模型:",list(fc.columns))

j=pd.concat([obs.rename('OBS'),fc],axis=1)
j=j[j.OBS.notna()]
j=j.loc[j.index>=fc.index.min()]
j=j.dropna(subset=list(fc.columns))
print(f"\n回测窗口: {j.index.min().date()} ~ {j.index.max().date()}  n={len(j)}  实测均值 {j.OBS.mean():.2f}°C\n")
print(f"{'模型/方法':<24}{'偏差':>9}{'MAE':>8}{'RMSE':>8}{'r':>8}{'残差sd':>9}")
rows=[]
for c in fc.columns:
    e=j.OBS-j[c]
    rows.append((c,e.mean(),e.abs().mean(),np.sqrt((e**2).mean()),np.corrcoef(j.OBS,j[c])[0,1],e.std()))
for r in sorted(rows,key=lambda x:x[2]):
    print(f"{r[0]:<24}{r[1]:>+9.2f}{r[2]:>8.2f}{r[3]:>8.2f}{r[4]:>8.3f}{r[5]:>9.2f}")

def rep(name,pred):
    e=j.OBS-pred
    print(f"{name:<24}{e.mean():>+9.2f}{e.abs().mean():>8.2f}{np.sqrt((e**2).mean()):>8.2f}{np.corrcoef(j.OBS,pred)[0,1]:>8.3f}{e.std():>9.2f}")
    return e

print("-"*70)
e=rep(">>> 全模型等权融合",j[fc.columns].mean(axis=1))
best=[r[0] for r in sorted(rows,key=lambda x:x[2])[:3]]
e3=rep(f">>> Top3融合({'+'.join(best)})",j[best].mean(axis=1))
b=e.mean(); sd=e.std(); j['ENSEMBLE_CORR']=j[fc.columns].mean(axis=1)+b
e2=rep(">>> 融合+偏差修正",j.ENSEMBLE_CORR)

clim=obs.loc['2018':'2025']
roll=clim.groupby(clim.index.dayofyear).mean().rolling(15,center=True,min_periods=5).mean()
base=pd.Series([roll.get(min(d.dayofyear,366),np.nan) for d in j.index],index=j.index)
rep(">>> 仅气候季节性基线",base)

j.to_csv('data/backtest_join.csv')
json.dump({'bias':round(float(b),3),'resid_sd':round(float(sd),3),'n':int(len(j)),
           'window':[str(j.index.min().date()),str(j.index.max().date())],
           'models':list(fc.columns)},open('data/model_calib.json','w'),indent=1)
print(f"\n校准参数: 网格+模型综合 bias={b:+.2f}°C, 残差sd={sd:.2f}°C (n={len(j)})")
