# app2.py — EV HUD (regex alias for temps, session-persistent data, raised distance/efficiency only)
import io, zipfile, time, math, re
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
import altair as alt

st.set_page_config(page_title="EV HUD", layout="wide")

# ========== 0) Page selection ==========
if "page" not in st.session_state:
    st.session_state.page = "main"  # 기본 메인 페이지


# ========== 1) Global CSS ==========
st.markdown("""
<style>
:root{
  --font:'Inter','Pretendard','Noto Sans KR',system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
}
html,body,.stApp,[data-testid="stAppViewContainer"],[data-testid="block-container"]{
  font-family:var(--font)!important;
}
[data-testid="block-container"]{ padding-top:24px; max-width:1200px; }

.badge{ padding:8px 14px; border-radius:999px; border:1px solid #0002; font-weight:800 }
.badge.off{ background:#d5d9e0; } .badge.onC{ background:#20ff9e; } .badge.onD{ background:#7db3ff; }

/* bottom temps: text-only */
.tempWrap{ text-align:center; color:#fff; }
.tempLabel{ font-size:13px; font-weight:800; opacity:.75; letter-spacing:.2px; }
.tempVal{ font-size:16px; font-weight:900; margin-top:6px; }

/* right column spacing under the power bar */
.right-links-spacer{ height:14px; }
</style>
""", unsafe_allow_html=True)

# ========== 2) Utils ==========
def safe_float(x, d=0.0):
    try:
        if isinstance(x,str): x=x.replace(',','').strip()
        return float(x)
    except: return d

def read_csv_smart(file_like, name_hint="uploaded.csv"):
    raw = file_like.read() if hasattr(file_like,"read") else file_like
    if not isinstance(raw,(bytes,bytearray)): raw = bytes(raw)
    def _try(buf, enc=None, sep=None):
        bio=io.BytesIO(buf); return pd.read_csv(bio, encoding=enc, sep=sep, engine="python")
    try: df=_try(raw,None,None)
    except UnicodeDecodeError: df=_try(raw,"cp949",None)
    if df.shape[1]==1 and isinstance(df.iloc[0,0],str) and ";" in df.iloc[0,0]:
        try: df=_try(raw,None,";")
        except UnicodeDecodeError: df=_try(raw,"cp949",";")
    return df, name_hint

def read_zip_first_csv(file):
    with zipfile.ZipFile(file) as z:
        names=[n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names: return None,None
        sel=st.sidebar.selectbox("Select CSV in ZIP", names, index=0)
        with z.open(sel) as f: return read_csv_smart(io.BytesIO(f.read()), sel)

def read_xlsx_smart(file):
    xls=pd.ExcelFile(file)
    sheet=st.sidebar.selectbox("Sheet", xls.sheet_names, index=0)
    return xls.parse(sheet), f"{file.name}[{sheet}]"

# ── 2-1) 컬럼 별칭 (정규식 우선 → 정확히 일치 백업) ──
ALIASES = {
    "time":      [r"^Time\s*\[s\]$", r"^time$"],
    "speed":     [r"^(Velocity|Speed)\s*\[km/h\]$", r"^speed$"],
    "soc":       [r"^(displayed\s*)?SoC\s*\[%\]$", r"^SOC\s*\[%\]$"],
    "bat_temp":  [r"^Battery\s*Temperature\s*\[(°C|℃)\]$", r"^Battery\s*Temp"],
    "volt":      [r"^Battery\s*Voltage\s*\[V\]$"],
    "curr":      [r"^Battery\s*Current\s*\[A\]$"],
    "regen":     [r"^Regenerative\s*Braking\s*Signal"],
    "whpk":      [r"^Wh_per_km$", r"^Wh/?km$"],
    "cumdist":   [r"^cumdist$", r"^Cumulative(_| )?Distance(_km)?$", r"^Distance_km$"],
    # === 온도 컬럼 ===
    "cabin_temp":   [r"^Cabin\s*Temperature\s*Sensor\s*\[(°C|℃)\]$", r"^Cabin\s*(Temperature|Temp)"],
    "ambient_temp": [r"^Ambient\s*(Temperature|Temp)\s*\[(°C|℃)\]$", r"^Outside\s*(Temperature|Temp)", r"^External\s*Temp"],
    # === 추가 항목 ===
    "power":     [r"^Power\s*\[kW\]$", r"^Battery\s*Power\s*\[kW\]$"],
    "dte":       [r"^DTE$", r"^DTE\s*\[km\]$", r"^Range\s*Remaining$", r"^remain_km$"],
        # === HVAC 관련 signal ===
    "heater_signal": [r"^Heater(_| )?signal$", r"^Heater\s*ONOFF$"],
    "aircon_signal": [r"^Aircon(_| )?signal$", r"^AC\s*ONOFF$"]
}

def _match_regex(df, pattern_list):
    cols = list(df.columns)
    for pat in pattern_list:
        rx = re.compile(pat, flags=re.IGNORECASE)
        for c in cols:
            if isinstance(c,str) and rx.search(c.strip()):
                return c
    return None

def col(df, key):
    c = _match_regex(df, ALIASES.get(key, []))
    if c: return c
    for pat in ALIASES.get(key, []):
        for cc in df.columns:
            if isinstance(cc, str) and cc.strip() == pat:
                return cc
    return None

# ======== 온도 컬럼 유일 매핑 유틸 ========
import re
TEMP_PATTERNS = {
    "cabin": [
        r"^Cabin\s*Temperature\s*Sensor\s*\[(°C|℃)\]$",
        r"^Cabin\s*(Temperature|Temp)"
    ],
    "ambient": [
        r"^Ambient\s*(Temperature|Temp)\s*\[(°C|℃)\]$",
        r"^Outside\s*(Temperature|Temp)", r"^External\s*Temp"
    ],
}
def _first_regex_match_not_used(df, patterns, used):
    cols = list(df.columns)
    for pat in patterns:
        rx = re.compile(pat, flags=re.IGNORECASE)
        for c in cols:
            if c in used: 
                continue
            if isinstance(c, str) and rx.search(c.strip()):
                return c
    return None
def map_temp_columns_unique(df):
    used = set(); out = {}
    cab = _first_regex_match_not_used(df, TEMP_PATTERNS["cabin"], used)
    if cab: out["cabin"] = cab; used.add(cab)
    else:   out["cabin"] = None
    amb = _first_regex_match_not_used(df, TEMP_PATTERNS["ambient"], used)
    if amb: out["ambient"] = amb; used.add(amb)
    else:   out["ambient"] = None
    return out
# ================================================

# ========== 3) Battery (mint wave) ==========
def battery_html(soc, temp_c, remain_kwh, total_kwh):
    s = max(0, min(100, safe_float(soc, 0)))
    W,H = 300,360
    top,bot = 110,14
    tank_h  = H - top - bot
    if s >= 99.5: empty_h, wave_up = 0, 0
    else:
        empty_h = int(tank_h * (100 - s) / 100.0)
        wave_up = int((100 - s) * 0.7)
    fill_top="#dff7ec"; fill_bot="#34c759"; wave1="#c9f3e4"; wave2="#a7ebd2"
    chip  = "#e7fff0" if safe_float(temp_c,25)<35 else "#ffe4db"
    return f"""
<style>
.bx{{width:{W}px;height:{H}px;position:relative;border-radius:24px;margin-top:26px;
    background:linear-gradient(180deg,#f9fffe,#f3fbf9); box-shadow:0 0 0 3px #a7f0ba inset, 0 4px 16px #7ee39d22;}}
.inner{{position:absolute;left:6px;right:6px;top:6px;bottom:6px;border-radius:20px;background:rgba(255,255,255,.9);box-shadow:inset 0 0 0 3px #7ce192;}}
.cap{{position:absolute;left:27%;top:-20px;width:46%;height:20px;border-radius:14px 14px 0 0;border:3px solid #90e7a6;border-bottom:none;background:#f2fff7;}}
.perc{{position:absolute;left:20px;top:16px;font-weight:900;font-size:30px;color:#0e2433;text-shadow:0 1px 0 #fff;}}
.kwh{{position:absolute;left:20px;top:52px;font-size:14px;color:#123a;}}
.chip{{position:absolute;right:14px;top:14px;padding:8px 14px;border-radius:14px;background:{chip}; border:1.5px solid #0002;font-weight:900;color:#113;}}
.fillBase{{position:absolute;left:14px;right:14px;top:{top}px;bottom:{bot}px;border-radius:18px;background:linear-gradient(180deg,{fill_top},{fill_bot}); z-index:0;}}
.liq{{position:absolute;left:14px;right:14px;top:{top}px;bottom:{bot}px;border-radius:18px;background:transparent;overflow:hidden;z-index:1;}}
.w1,.w2{{position:absolute;left:0;bottom:-2px;width:200%;height:130%;pointer-events:none}}
.w1 path{{fill:{wave1}}} .w2 path{{fill:{wave2}}}
.w1{{animation:mv 7s linear infinite;opacity:.75;transform:translateY(-{wave_up}px)}}
.w2{{animation:mv 11s linear infinite reverse;opacity:.60;transform:translateY(-{wave_up}px)}}
@keyframes mv{{from{{transform:translateX(0)}}to{{transform:translateX(-50%)}}}}
.mask{{position:absolute;left:14px;right:14px;top:{top}px;height:{empty_h}px;border-radius:18px 18px 0 0;background:rgba(255,255,255,.96);pointer-events:none;z-index:2;}}
</style>
<div class="bx"><div class="cap"></div><div class="inner">
  <div class="perc">{int(round(s))}%</div>
  <div class="kwh">{safe_float(remain_kwh):.2f} kWh / {safe_float(total_kwh):.1f} kWh</div>
  <div class="chip">{safe_float(temp_c):.1f} ℃</div>
  <div class="fillBase"></div>
  <div class="liq">
    <svg class="w1" viewBox="0 0 1200 120" preserveAspectRatio="none">
      <path d="M0,60 C150,110 350,30 600,80 C850,130 1050,50 1200,90 V120 H0 Z"/></svg>
    <svg class="w2" viewBox="0 0 1200 120" preserveAspectRatio="none">
      <path d="M0,50 C200,0 400,100 600,60 C800,20 1000,90 1200,40 V120 H0 Z"/></svg>
  </div>
  <div class="mask"></div>
</div></div>
"""

# ========== 4) Speedometer ==========
def speedometer_html(speed, vmax=140):
    v = max(0.0, min(float(vmax), safe_float(speed,0)))
    th = math.radians(180.0 - 180.0*(v/vmax))
    cx,cy,r = 280,220,200
    x = cx + r*math.cos(th); y = cy - r*math.sin(th)
    return f"""
<div style="position:relative;width:100%;max-width:560px;height:300px;margin:0 auto;">
<svg viewBox="0 0 560 300">
  <defs>
    <linearGradient id="g1" x1="0" x2="1"><stop offset="0%" stop-color="#8EE3F5"/>
      <stop offset="100%" stop-color="#6AA6FF"/></linearGradient>
    <filter id="glow"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  </defs>
  <path d="M80 220 A200 200 0 0 1 480 220" fill="none" stroke="#ecf1f6" stroke-width="28"/>
  <path d="M80 220 A200 200 0 0 1 480 220" fill="none" stroke="url(#g1)" stroke-width="14" filter="url(#glow)"/>
  <line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="#ff6e6e" stroke-width="6" stroke-linecap="round"/>
  <circle cx="{cx}" cy="{cy}" r="10" fill="#333"/>
</svg>
</div>
"""

# ========== 5) Sidebar: data (session persistent) ==========
st.sidebar.header("Data")


up = st.sidebar.file_uploader("Upload (.zip/.csv/.xlsx)", type=["zip","csv","xlsx"])

df, name = None, None

# 업로드 처리
if up is not None:
    if up.name.lower().endswith(".zip"):
        df, name = read_zip_first_csv(up)
    elif up.name.lower().endswith(".csv"):
        df, name = read_csv_smart(up, up.name)
    else:
        df, name = read_xlsx_smart(up)
    if df is not None:
        df.columns = df.columns.str.strip()
        st.session_state.raw_df = df
        st.session_state.data_name = name
        st.session_state.df = df

elif "raw_df" in st.session_state:
    df = st.session_state.df
    name = st.session_state.get("data_name", "session_df")

elif "df" in st.session_state and isinstance(st.session_state.df, pd.DataFrame):
    df = st.session_state.df
    name = st.session_state.get("data_name", "session_df")

# 기본 더미 데이터
if df is None:
    N = 1500
    usable_kwh = 18.8  # 배터리 가용용량(kWh)
    df = pd.DataFrame({
        "Time [s]": [i * 0.1 for i in range(N)],
        "Velocity [km/h]": [abs(40 + 25 * math.sin(i / 18)) for i in range(N)],
        "displayed SoC [%]": [max(0, 85 - i * 0.02) for i in range(N)],
        "Battery Temperature [°C]": [24 + 6 * math.sin(i / 70) for i in range(N)],
        "Battery Voltage [V]": [355] * N,
        "Battery Current [A]": [18 - 28 * math.sin(i / 45) for i in range(N)],
        "Wh_per_km": [130 + 25 * math.sin(i / 80) for i in range(N)],
        "Regenerative Braking Signal ": [1 if math.sin(i / 35) < -0.8 else 0 for i in range(N)],
        "Cabin Temperature Sensor [°C]": [22 + 2 * math.sin(i / 120) for i in range(N)],
        "Ambient Temperature [°C]": [15 + 1.5 * math.sin(i / 300) for i in range(N)],
        "Heater_Power [kW]": [2.5] * N,
        "AirCon Power [kW]": [1.8] * N,
        "Driving Resistance Power [W]": [500] * N,
        "Actual Driving Power Consumption [W]": [1200] * N
        
    })
    df["Heater_signal"] = (df["Heater_Power [kW]"] != 0).astype(int)
    df["Aircon_signal"] = (df["AirCon Power [kW]"] != 0).astype(int)
    # Power [kW] 계산
    df["Power [kW]"] = (df["Battery Voltage [V]"] * df["Battery Current [A]"]) / 1000.0

    # remain_km 계산
    df["DTE"] = (
        (usable_kwh * df["displayed SoC [%]"] / 100.0) * 1000.0
        / df["Wh_per_km"]
    )

    # cumdist (누적 거리) 계산
    dt = df["Time [s]"].diff().fillna(0)
    dist = dt / 3600 * df["Velocity [km/h]"]
    df["cumdist"] = dist.cumsum()

    name = "demo"
    st.session_state.df = df
    st.session_state.data_name = name

st.sidebar.write(f"**Loaded:** {name}")
# ========== Trip_id 드롭다운 + 필터링 ==========
trip_cols = []
if df is not None:
    df.columns = df.columns.str.strip()
    trip_cols = [c for c in df.columns if 'sourcefile' in c.lower()]

if len(trip_cols) == 0:
    st.sidebar.error("No 'trip_id' column found.")
    selected_trip = None
else:
    selected_trip_col = trip_cols[0]
    if "raw_df" in st.session_state:
        trip_source_df = st.session_state.raw_df
    else:
        trip_source_df = df

    trips = ["All"] + list(trip_source_df[selected_trip_col].unique())
    selected_trip = st.sidebar.selectbox("Select Trip ID", trips)

    if selected_trip != "All":
        if "raw_df" in st.session_state:
            filtered_df = st.session_state.raw_df[st.session_state.raw_df[selected_trip_col] == selected_trip].copy()
            st.session_state.df = filtered_df
            del st.session_state.raw_df
            df = filtered_df
        else:
            filtered_df = df[df[selected_trip_col] == selected_trip].copy()
            st.session_state.df = filtered_df
            df = filtered_df
    else:
        if "raw_df" in st.session_state:
            st.session_state.df = st.session_state.raw_df
            df = st.session_state.raw_df
        else:
            st.sidebar.warning("원본 데이터가 없어 전체 보기가 불가능합니다. 파일을 다시 업로드 해주세요.")

# 온도 컬럼 유일 매핑 (세션 저장)
st.session_state.temp_cols = map_temp_columns_unique(df)
st.session_state.df = df

# ========== 6) Playback state ==========
if "idx" not in st.session_state: st.session_state.idx=0
if "play" not in st.session_state: st.session_state.play=False
if "fps"  not in st.session_state: st.session_state.fps=30
if "step" not in st.session_state: st.session_state.step=1
if "loop" not in st.session_state: st.session_state.loop=True
if "bars_mem" not in st.session_state: st.session_state.bars_mem = {}
if "idx_pending" in st.session_state:
    st.session_state.idx = st.session_state.idx_pending
    del st.session_state["idx_pending"]

st.sidebar.slider("Frame", 0, len(df)-1, key="idx", step=1)
cA,cB,_ = st.sidebar.columns(3)
cA.button("▶/⏸", on_click=lambda: st.session_state.update(play=not st.session_state.play))
cB.button("⏮", on_click=lambda: (st.session_state.update(idx_pending=0), st.rerun()))
st.sidebar.slider("FPS", 1, 50, key="fps")
st.sidebar.slider("Frames/step", 1, 20, key="step")
st.sidebar.checkbox("Loop", key="loop")
invert_hint = st.sidebar.checkbox("Invert current polarity", value=False)

# ========== 7) Current frame + states ==========
row=df.iloc[st.session_state.idx]
def getv(k,d=None): 
    c=col(df,k); 
    return row[c] if c and c in df.columns else d

soc  = getv("soc",50.0); spd  = getv("speed",0.0); btmp = getv("bat_temp",25.0)
volt = getv("volt",350.0); curr = getv("curr",0.0)
regen_sig=getv("regen",0)

def infer_polarity(df, curr_col, regen_col):
    if curr_col is None: return 1
    s = pd.to_numeric(df[curr_col], errors="coerce").dropna()
    if regen_col and regen_col in df.columns:
        m = df[regen_col] > 0.5
        sample = pd.to_numeric(df.loc[m, curr_col], errors="coerce").dropna()
        if len(sample) < 10: sample = s
    else: sample = s
    return -1 if (sample < 0).mean() > 0.6 else 1

POL_AUTO = infer_polarity(df, col(df,"curr"), col(df,"regen"))
POL = -POL_AUTO if invert_hint else POL_AUTO

spd_v = safe_float(spd,0.0)
reg_v = safe_float(regen_sig,0.0)
charge_current = POL * safe_float(curr,0.0)

V_THRESH = 0.8; REGEN_I  = 1.0; PLUG_I   = 1.5
is_stopped = spd_v <= V_THRESH; is_moving  = spd_v > V_THRESH
charging = is_stopped and (charge_current > PLUG_I)
regen    = is_moving  and ((reg_v > 0.5) or (charge_current > REGEN_I))
driving  = is_moving  and not charging


# -------------------------------------------------------------------------------
whpk_col = col(df, "whpk")
if whpk_col:
    drv_whpk = abs(safe_float(row[whpk_col], 150.0))
else:
    v = max(0.5, spd_v)
    drv_whpk = abs(safe_float(volt) * safe_float(curr)) / v

dist_col = col(df, "cumdist")
if dist_col:
    dist_km = abs(safe_float(row[dist_col], 0.0))  # 기본값 0.0으로 설정
else:
    dist_km = 0.0  # cumdist 컬럼 없으면 0으로 초기화

remain_col = col(df, "dte")
if remain_col:
    remain_km = abs(safe_float(row[remain_col], 0.0))  # 기본값 0.0
else:
    remain_km = 0.0  # remain_km 컬럼 없으면 0으로 초기화

def go_back():
    st.session_state.page = "main"

# 항상 페이지 맨 끝의 좌측 하단에 놓이는 Back 버튼
def back_button(label="Back", pad_px: int = 24, left_w: float = 0.08):
    # 콘텐츠와 버튼 사이 여백
    st.markdown(f"<div style='height:{pad_px}px'></div>", unsafe_allow_html=True)

    # 왼쪽 컬럼 비율(left_w)을 줄이면 더 왼쪽으로 붙음 (예: 0.1, 0.05 …)
    left, _ = st.columns([left_w, 1 - left_w])
    with left:
        if st.button(label, key=f"back_{st.session_state.page}", use_container_width=True):
            st.session_state.page = "main"
            st.rerun()


USABLE=18.8
remain_kwh = USABLE*(safe_float(soc)/100.0)


# 실제 주행 가능 거리 계산
heater_factor = 0.25
aircon_factor = 0.063

df_full = st.session_state.df  # 전체 df
heater_col = col(df_full, "heater_signal")
aircon_col = col(df_full, "aircon_signal")


if "menu" not in st.session_state:
    st.session_state.menu = "Details"

# ===== 메뉴 클릭 콜백 정의 =====
def set_menu(menu_name):
    st.session_state.menu = menu_name

# ---- time window helper (10분 창 계산용) ----
def get_time_window_df(df_in, time_col, idx, seconds=600):
    t = pd.to_numeric(df_in[time_col], errors="coerce")
    t_now = float(t.iloc[idx])
    t_min, t_max = float(t.min()), float(t.max())
    start = max(t_min, t_now - seconds); end = t_now
    win = df_in.loc[(t>=start)&(t<=end)].copy()
    win[time_col] = pd.to_numeric(win[time_col], errors="coerce")
    return win, start, end

if st.session_state.page == "main":
    # ========== 8) Layout (L / M / R) — L, M ==========
    L, M, R = st.columns([1.05, 1.35, 0.9])

    with L:
        st.markdown("<div style='height:70px'></div>", unsafe_allow_html=True)
        st.markdown(battery_html(soc, btmp, remain_kwh, USABLE), unsafe_allow_html=True)

    with M:
        def drive_header(charging: bool, driving: bool) -> str:
            # 상태 결정
            if charging:
                text = "C H A R G I N G"
                color = "#0a0"  # 녹색
            elif driving:
                text = "D R I V I N G"
                color = "#0a0"
            else:
                text = "Idle"
                color = "#888"  # 회색

            return f"""
            <div style="display:flex; justify-content:center; gap:10px;">
                <span class="badge" style="
                    background-color:{color};
                    color:white;
                    padding:5px 12px;
                    border-radius:5px;
                    font-weight:bold;
                ">{text}</span>
            </div>
            """

        st.markdown(drive_header(charging, driving), unsafe_allow_html=True)

        st.markdown(
            f"<div style='text-align:center; font-size:72px; font-weight:900; margin-bottom:6px;'>{safe_float(spd):.1f} km/h</div>",
            unsafe_allow_html=True
        )
        st.markdown(speedometer_html(spd, vmax=140), unsafe_allow_html=True)

        # Distance / Efficiency만 위로 올림 (온도는 영향 안 주도록 spacer 보정)
        st.markdown(
            f"""
            <div style="display:flex; flex-direction: column; align-items: center; gap: 2px; margin-top:-120px; position:relative; z-index:2;">
                <div style="font-weight:700; font-size:18px;">Distance (km)</div>
                <div style="font-size:22px;">{dist_km:.2f}</div>
                <div style="font-weight:700; font-size:18px; margin-top:2px;">Efficiency (Wh/km)</div>
                <div style="font-size:22px;">{drv_whpk:.2f}</div>
            </div>
            """,
            unsafe_allow_html=True
        )
        st.markdown("<div style='height:35px'></div>", unsafe_allow_html=True)  # 보호 spacer

    with R: 
        st.markdown("<div style='height:90px'></div>", unsafe_allow_html=True)
        idx = st.session_state.get("idx", len(df)-1)
        heater_on = bool(df[heater_col].iloc[idx]) if heater_col else False
        aircon_on = bool(df[aircon_col].iloc[idx]) if aircon_col else False
        remain_km_adj = remain_km
        if heater_on:
            remain_km_adj *= (1 - heater_factor)
        if aircon_on:
            remain_km_adj *= (1 - aircon_factor)

        st.markdown(
            f"""
            <div style="
                margin-bottom:8px;
                padding:10px;
                border-radius:10px;
                background:#e9f4ff;
                border:2px solid #7fb7ff;
                text-align:center;
                font-weight:800;
            ">
                <div style="font-size:12px;opacity:.7; color:#333333;">max 200 km</div>
                <div style="font-size:20px; color:#222222; font-weight:900;">Remaining Range</div>
                <div style="font-size:22px;color:#1f5fff;">{remain_km_adj:.1f} km</div>
                <div style="margin-top:8px; font-size:16px;">
                    <span style="color:red;">Heater: {'🟢' if heater_on else '⚪'}</span><br>
                    <span style="color:blue;">Aircon: {'🟢' if aircon_on else '⚪'}</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )
            

        st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)

        # Power bar (데이터 없으면 V*I 추정) — 안전한 fallback
        idx_now = st.session_state.idx
        try:
            power_kw = safe_float(df["Power [kW]"].iloc[idx_now])
        except Exception:
            power_kw = (safe_float(volt) * safe_float(curr)) / 1000.0

        max_power = 150  # 최대 바 길이 기준(kW)
        bar_width = max(0, min(100, (abs(power_kw) / max_power) * 100))
        bar_color = "#1f5fff" if power_kw < 0 else "#20ff9e"

        st.markdown(f"""
        <div>
        <div style="font-weight:700; font-size:12px;">Power (kW)</div>
        <div style="background:#ddd; border-radius:5px; overflow:hidden; height:20px;">
            <div style="width:{bar_width}%; height:100%; background:{bar_color};"></div>
        </div>
        <div style="font-size:12px; margin-top:4px;">{power_kw:.2f} kW</div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("<div style='height:40px;'></div>", unsafe_allow_html=True)

        if st.button("🔹 Power details"):
            st.session_state.page = "sub1"
            st.rerun()  # 클릭 즉시 서브 페이지로 이동
    
        st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)

        st.button(label=("🟢" if regen else "⚪") + " Regen", on_click=lambda: st.session_state.update(page="sub2"))
    # =========================
    # 9) Bottom temps (unique-mapped)
    # =========================
    temp_map = st.session_state.get("temp_cols", {})

    cab_col = temp_map.get("cabin")
    amb_col = temp_map.get("ambient")

    cab = safe_float(row[cab_col]) if cab_col else float("nan")
    amb = safe_float(row[amb_col]) if amb_col else float("nan")

    bL, bM, bR = st.columns([1.3, 2, 1])  # 가운데 칼럼 bM을 넓게

    with bM:
        st.markdown(f"""
        <div style="display:flex; justify-content:center; gap:60px;">
            <div class='tempWrap'>
                <div class='tempLabel'>Cabin Temp</div>
                <div class='tempVal'>{'' if pd.isna(cab) else f'{cab:.1f} ℃'}</div>
            </div>
            <div class='tempWrap'>
                <div class='tempLabel'>Ambient Temp</div>
                <div class='tempVal'>{'' if pd.isna(amb) else f'{amb:.1f} ℃'}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

elif st.session_state.page == "sub1":
    # =========================
    # Subheader / Layout
    # =========================
    st.markdown("""
    <style>
    .tabs {display: flex; gap: 20px; margin-bottom: 10px;}
    .tab {
        padding: 8px 20px;
        border: 1px solid #0a0;
        border-radius: 5px 5px 0 0;
        background-color: #0a0;
        color: white;
        font-weight: bold;
    }
    </style>
""", unsafe_allow_html=True)
    st.markdown('<div class="tabs"><div class="tab">Energy</div></div>', unsafe_allow_html=True)
    st.markdown("<h2 style='text-align:center;'></h2>", unsafe_allow_html=True)

    col1, col2 = st.columns([1, 5])

    with col1:
        st.write("### Menu")
        menus = ["Details", "Flow", "History"]

        for m in menus:
            if st.session_state.menu == m:
                st.markdown(f"""
                    <div style="
                        background-color:#0a0;
                        color:white;
                        padding:5px;
                        border-radius:5px;
                        font-weight:bold;
                        cursor:pointer;
                        margin-bottom: 5px;
                    ">{m}</div>
                """, unsafe_allow_html=True)
            else:
                st.button(m, key="sub_"+m, on_click=set_menu, args=(m,))

        st.markdown("<div style='height:250px;'></div>", unsafe_allow_html=True)

    with col2:
        st.write(f"### {st.session_state.menu} Content")

        # ===== 메뉴별 콘텐츠 =====
        if st.session_state.menu == "Details":

            # ---- 1) 세션 가드 ----
            if "df" not in st.session_state or st.session_state.df is None or len(st.session_state.df) == 0:
                st.info("메인 페이지(app2.py)에서 데이터를 먼저 로드해 주세요.")
                st.stop()

            df = st.session_state.df.copy()

            # ---- 2) 현재 idx 기준 데이터 ----
            idx = int(st.session_state.get("idx", len(df)-1))
            idx = max(0, min(idx, len(df)-1))
            df_now = df.iloc[idx]

            # ---- 3) Bar용 데이터 준비 ----
            powers = {
                "Heater Power [kW]": df_now.get("Heater_Power [kW]", 0),
                "AirCon Power [kW]": df_now.get("AirCon Power [kW]", 0),
                "Driving Resistance Power [kW]": df_now.get("Driving Resistance Power [W]", 0) / 1000,
                "Actual Driving Power Consumption [kW]": df_now.get("Actual Driving Power Consumption [W]", 0) / 1000
            }

            # ---- 4) Plotly Bar 차트 ----
            bar_df = pd.DataFrame({"Power": list(powers.values())}, index=list(powers.keys()))

            fig = go.Figure(go.Bar(
                x=bar_df["Power"],
                y=bar_df.index,
                orientation='h',
                marker_color='limegreen'
            ))

            fig.update_layout(
                title="Current Power Distribution",
                xaxis_title="Power (kW)",
                yaxis_title="",
                plot_bgcolor='black',
                paper_bgcolor='black',
                font_color='white',
                height=300,
                margin=dict(l=20, r=20, t=40, b=20)
            )

            st.plotly_chart(fig, use_container_width=True)                

        elif st.session_state.menu == "Flow":
            st.set_page_config(page_title="Power details", layout="wide")

            # ---- (1) 세션 가드 ----
            if "df" not in st.session_state or st.session_state.df is None or len(st.session_state.df) == 0:
                st.info("메인 페이지(app2.py)에서 데이터를 먼저 로드해 주세요.")
                st.stop()

            df = st.session_state.df.copy()

            # ===== 데이터 준비 =====
            def _num(s):
                return pd.to_numeric(s, errors="coerce")

            if "Power [kW]" in df.columns:
                df["power_kw"] = _num(df["Power [kW]"])
            elif {"Battery Voltage [V]", "Battery Current [A]"} <= set(df.columns):
                df["power_kw"] = _num(df["Battery Voltage [V]"]) * _num(df["Battery Current [A]"]) / 1000.0
            else:
                st.warning("전력 계산에 필요한 컬럼이 없습니다. (Power [kW] 또는 V/A)")
                st.stop()

            # 시간축
            time_col = "Time [s]" if "Time [s]" in df.columns else None
            if time_col is None:
                df["index_time"] = np.arange(len(df)) * 0.1
                time_col = "index_time"

            # ---- (3) KPI (현재 idx까지 기준) ----
            idx = int(st.session_state.get("idx", len(df) - 1))
            idx = max(0, min(idx, len(df) - 1))
            df_now = df.iloc[:idx + 1]  # 현재 프레임까지

            now_kw = float(_num(df_now["power_kw"].iloc[-1]) if len(df_now) else np.nan)
            mean_kw = float(_num(df_now["power_kw"]).mean()) if len(df_now) else np.nan
            peak_kw = float(_num(df_now["power_kw"]).max()) if len(df_now) else np.nan

            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric("Current Power", f"{now_kw:.1f} kW" if pd.notna(now_kw) else "—")
            with c2:
                st.metric("Mean Power", f"{mean_kw:.1f} kW" if pd.notna(mean_kw) else "—")
            with c3:
                st.metric("Peak Power", f"{peak_kw:.1f} kW" if pd.notna(peak_kw) else "—")

            st.divider()

            # ---- (4) 시계열/분포 그래프 (현재 idx까지) ----
            # 1분(60초) 이전 데이터만
            d1 = df_now[[time_col, "power_kw"]].copy()
            d1[time_col] = pd.to_numeric(d1[time_col], errors="coerce")
            d1["power_kw"] = pd.to_numeric(d1["power_kw"], errors="coerce")
            d1 = d1.dropna(subset=[time_col, "power_kw"])

            idx = int(st.session_state.get("idx", len(df)-1))
            idx = max(0, min(idx, len(df)-1))
            current_time = df[time_col].iloc[idx]
            start_time = max(0, current_time - 60)

            df_window = df.iloc[:idx+1].copy()
            df_window = df_window[(df_window[time_col] >= start_time) & (df_window[time_col] <= current_time)]
            df_window[time_col] = pd.to_numeric(df_window[time_col], errors="coerce")
            df_window["power_kw"] = pd.to_numeric(df_window["power_kw"], errors="coerce")
            df_window = df_window.dropna(subset=[time_col, "power_kw"])

            if len(df_window) > 0:
                fig = px.line(
                    df_window,
                    x=time_col,
                    y="power_kw",
                    markers=False,
                    title=f"Power (kW) last 1 min"
                )
                fig.update_traces(line_color='limegreen', line_width=2)
                fig.update_layout(
                    plot_bgcolor='black',
                    paper_bgcolor='black',
                    font_color='white',
                    height=240,
                    margin=dict(l=20, r=20, t=20, b=20),
                    xaxis_title='Time (s)',
                    yaxis_title='Power (kW)'
                )   
                st.plotly_chart(fig, use_container_width=True)

                st.write("")
                hist = alt.Chart(d1).mark_bar().encode(
                    x=alt.X("power_kw:Q", bin=alt.Bin(maxbins=40), title="Power (kW)"),
                    y=alt.Y("count()", title="Count")
                ).properties(height=240)
                st.altair_chart(hist, use_container_width=True)
                
        else:  # History  ← 이 블록 전체를 교체
            # ====== 데이터 가드 ======
            if "df" not in st.session_state or st.session_state.df is None or len(st.session_state.df) == 0:
                st.info("메인 페이지에서 데이터를 먼저 로드해 주세요.")
                st.stop()

            dfh = st.session_state.df.copy()

            # ▶ 현재 프레임까지만 사용(실시간 연동 핵심)
            idx = int(st.session_state.get("idx", len(dfh) - 1))
            idx = max(0, min(idx, len(dfh) - 1))
            dfh = dfh.iloc[:idx + 1].copy()
            if len(dfh) < 2:
                st.info("표시할 데이터가 없습니다.")
                st.stop()

            # ====== 유틸 ======
            def _num(s): return pd.to_numeric(s, errors="coerce")

            # 컬럼 추출
            time_col = col(dfh, "time") or ("Time [s]" if "Time [s]" in dfh.columns else None)
            speed_col = col(dfh, "speed")
            curr_col  = col(dfh, "curr")
            volt_col  = col(dfh, "volt")
            dist_col  = col(dfh, "cumdist")

            # 시간축 필수
            if time_col is None:
                st.warning("시간(Time [s]) 컬럼이 없어 History를 계산할 수 없습니다.")
                st.stop()

            # 정렬/정규화
            dfh[time_col] = _num(dfh[time_col])
            dfh = dfh.sort_values(time_col).dropna(subset=[time_col]).reset_index(drop=True)
            dt = dfh[time_col].diff().fillna(0).clip(lower=0)   # 초, 음수 방어

            # 전력 kW
            if "Power [kW]" in dfh.columns:
                dfh["power_kw"] = _num(dfh["Power [kW]"])
            elif {"Battery Voltage [V]", "Battery Current [A]"} <= set(dfh.columns):
                dfh["power_kw"] = _num(dfh["Battery Voltage [V]"]) * _num(dfh["Battery Current [A]"]) / 1000.0
            elif volt_col and curr_col:
                dfh["power_kw"] = _num(dfh[volt_col]) * _num(dfh[curr_col]) / 1000.0
            else:
                dfh["power_kw"] = np.nan

            # 누적거리 km
            if dist_col:
                dist_km = _num(dfh[dist_col]).fillna(method="ffill")
            elif speed_col:
                v = _num(dfh[speed_col]).fillna(0)              # km/h
                dist_km = (dt / 3600.0 * v).cumsum()            # km
            else:
                st.warning("거리(cumdist) 또는 속도(Velocity [km/h])가 없어 히스토리 그래프를 그릴 수 없습니다.")
                st.stop()

            # 에너지 적분 (kWh)
            p = _num(dfh["power_kw"]).fillna(0.0)
            discharge_kw   = np.maximum(0.0, p)                 # 양수
            regen_kw       = np.maximum(0.0, -p)                 # 음수 절댓값
            discharge_kwh  = (discharge_kw * dt / 3600.0).cumsum()
            regen_kwh      = (regen_kw     * dt / 3600.0).cumsum()
            net_kwh        = discharge_kwh - regen_kwh

            # KPI
            total_dist = float(dist_km.iloc[-1]) if len(dist_km) else 0.0
            net_e_kwh  = float(net_kwh.iloc[-1])  if len(net_kwh) else 0.0
            avg_eff_whpk = abs(net_e_kwh * 1000.0) / total_dist if total_dist > 1e-6 else np.nan
            peak_power_kw = float(np.nanmax(p)) if len(p) else np.nan

            if curr_col:
                peak_regen_a = float(np.nanmin(_num(dfh[curr_col])))  # 회생이 음수면 음수로 표시
            elif volt_col and np.isfinite(peak_power_kw):
                vmean = float(np.nanmean(_num(dfh[volt_col])))
                peak_regen_a = float(-np.nanmax(regen_kw) * 1000.0 / vmean) if vmean > 1 else np.nan
            else:
                peak_regen_a = np.nan

            # ====== 정렬용 CSS (gap/여백 픽셀 조정 가능) ======
            st.markdown("""
            <style>
            :root{
              --hist-gap: 16px;   /* KPI 카드 사이 간격 */
              --hist-mb:  14px;   /* KPI 아래 여백 */
            }
            .hist-kpis{
              display:grid;
              grid-template-columns:repeat(4, minmax(0,1fr));
              gap:var(--hist-gap);
              margin:6px 0 var(--hist-mb) 0;
              align-items:stretch;
            }
            .hist-card{
              background:#0b0f14;
              border:1px solid #ffffff22;
              border-radius:12px;
              padding:14px 16px;
            }
            .hist-label{font-size:12px; opacity:.7;}
            .hist-val{font-weight:800; font-size:24px; line-height:1.15;}
            </style>
            """, unsafe_allow_html=True)

            # ====== KPI 한 줄(정렬 고정) ======
            st.markdown(f"""
            <div class="hist-kpis">
              <div class="hist-card">
                <div class="hist-label">Total Distance</div>
                <div class="hist-val">{total_dist:.2f} km</div>
              </div>
              <div class="hist-card">
                <div class="hist-label">Avg Efficiency</div>
                <div class="hist-val">{'' if not np.isfinite(avg_eff_whpk) else f'{avg_eff_whpk:.1f} Wh/km'}</div>
              </div>
              <div class="hist-card">
                <div class="hist-label">Peak Power</div>
                <div class="hist-val">{'' if not np.isfinite(peak_power_kw) else f'{peak_power_kw:.1f} kW'}</div>
              </div>
              <div class="hist-card">
                <div class="hist-label">Peak Regen</div>
                <div class="hist-val">{'' if not np.isfinite(peak_regen_a) else f'{peak_regen_a:.1f} A'}</div>
              </div>
            </div>
            """, unsafe_allow_html=True)

            # ====== 누적 에너지 vs 거리 ======
            dplot = pd.DataFrame({
                "Distance (km)": dist_km.values,
                "Discharge kWh": discharge_kwh.values,
                "Regen kWh":     regen_kwh.values,
                "Net kWh":       net_kwh.values,
            })

            if len(dplot) == 0 or not np.isfinite(total_dist):
                st.warning("표시할 데이터가 없습니다.")
            else:
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=dplot["Distance (km)"], y=dplot["Discharge kWh"],
                                         mode="lines", name="Discharge kWh"))
                fig.add_trace(go.Scatter(x=dplot["Distance (km)"], y=dplot["Regen kWh"],
                                         mode="lines", name="Regen kWh"))
                fig.add_trace(go.Scatter(x=dplot["Distance (km)"], y=dplot["Net kWh"],
                                         mode="lines", name="Net kWh"))
                fig.update_layout(
                    plot_bgcolor='black', paper_bgcolor='black', font_color='white',
                    height=360, margin=dict(l=20, r=20, t=10, b=30),
                    xaxis_title="Distance (km)", yaxis_title="Energy (kWh)",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0)
                )
                st.plotly_chart(fig, use_container_width=True)

    st.button("Back", on_click=go_back)
       
    
# =====================================================================
# SUB2: Regen — 10분 area + live autoplay (교체된 코드)
# =====================================================================
elif st.session_state.page == "sub2":
    dfr = st.session_state.df.copy()

    # 시간 컬럼 확보
    time_col = "Time [s]" if "Time [s]" in dfr.columns else None
    if time_col is None:
        dfr["index_time"] = np.arange(len(dfr)) * 0.1
        time_col = "index_time"

    # 전력(kW) 계산 (없으면 V*I/1000)
    def _num(s): return pd.to_numeric(s, errors="coerce")
    if "Power [kW]" in dfr.columns:
        power_kw = _num(dfr["Power [kW]"])
    elif {"Battery Voltage [V]", "Battery Current [A]"} <= set(dfr.columns):
        power_kw = _num(dfr["Battery Voltage [V]"]) * _num(dfr["Battery Current [A]"]) / 1000.0
    else:
        power_kw = pd.Series([np.nan]*len(dfr))

    dfr["power_kw"] = power_kw
    # 회생 전력은 음수 구간의 절댓값(=발생한 회생 파워)
    dfr["regen_kw"] = np.maximum(0.0, -pd.to_numeric(dfr["power_kw"], errors="coerce"))

    # 현재 프레임 기준 10분 윈도우
    idx = int(st.session_state.get("idx", len(dfr) - 1))
    idx = max(0, min(idx, len(dfr) - 1))
    win, _, _ = get_time_window_df(dfr, time_col, idx, seconds=600)
    win = win.dropna(subset=["regen_kw", time_col]).copy()

    # 회생 에너지(kWh) 적분
    if len(win) > 1:
        t = pd.to_numeric(win[time_col], errors="coerce").values
        dt = np.diff(t, prepend=t[0])  # 초
        e_kwh = float(np.sum(win["regen_kw"].values * dt) / 3600.0)
    else:
        e_kwh = 0.0

    st.markdown(f"### Regen (last 10 min) — recovered energy: **{e_kwh:.2f} kWh**")

    if len(win) > 0:
        fig = px.line(win, x=time_col, y="regen_kw",
                      labels={time_col:"Time (s)", "regen_kw":"Regen Power (kW)"})
        fig.update_traces(fill='tozeroy')
        fig.update_layout(height=340, margin=dict(l=20,r=20,t=10,b=10),
                          plot_bgcolor="black", paper_bgcolor="black", font_color="white")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("표시할 데이터가 없습니다.")

    

    st.button("Back", on_click=go_back)

# ========== 10) Autoplay ==========
if st.session_state.play:
    time.sleep(1.0 / max(1, int(st.session_state.fps)))
    nxt = st.session_state.idx + max(1, int(st.session_state.step))
    if nxt >= len(df):
        if st.session_state.loop:
            nxt = 0
        else:
            nxt = len(df) - 1
            st.session_state.play = False
    st.session_state["idx_pending"] = nxt
    st.rerun()
