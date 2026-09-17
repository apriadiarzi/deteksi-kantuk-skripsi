import streamlit as st
import json
from datetime import datetime, date
from urllib.parse import unquote
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh
from database import (
    get_sessions_by_username, get_events_by_session,
    resolve_guest_user, GUEST_STORAGE_KEY, GUEST_COOKIE_VALUE,
    get_username_by_token, clear_session_token, AUTH_STORAGE_KEY,
)

st.set_page_config(
    page_title="Histori Berkendara",
    page_icon="https://cdn-icons-png.flaticon.com/512/1464/1464723.png",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ───────────────────────────────────────────────────────────────────────
# Token & arah desain sama dengan halaman utama: navy/blue cool-tone.
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');

:root {
    --bg:            #123A63;
    --surface:       #17477A;
    --primary:       #0068FF;
    --accent:        #00B8D9;
    --accent-bright: #00D4FF;
    --fg:            #F5F7FA;
    --muted:         #9FB3C8;
    --border:        rgba(159,179,200,0.16);
    --alert:         #FF4D6D;
    --radius-sm: 6px;
    --radius-md: 10px;
}

html, body, [class*="css"] { font-family: 'IBM Plex Sans', sans-serif; color: var(--fg); }
h1, h2, h3 { font-family: 'Space Grotesk', sans-serif; }
.mono { font-family: 'IBM Plex Mono', monospace; }

#MainMenu, footer, [data-testid="stToolbar"] { visibility: hidden; }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stDecoration"] { display: none; }

/* Background disamakan dengan halaman utama: warna dasar + dot-grid halus. */
[data-testid="stAppViewContainer"] {
    background-color: var(--bg);
    background-image:
        radial-gradient(ellipse at 50% 0%, rgba(255,255,255,0.055), transparent 62%),
        radial-gradient(rgba(255,255,255,0.05) 1px, transparent 1px);
    background-size: 100% 100%, 22px 22px;
    background-attachment: fixed;
}
[data-testid="stAppViewContainer"] > .main > div { max-width: 780px; margin: 0 auto; }
[data-testid="stSidebar"] { background: var(--surface); border-right: 1px solid var(--border); }
[data-testid="stSidebar"] * { color: var(--fg); }

/* Header */
.app-header {
    display: flex; align-items: center; gap: 10px;
    padding-bottom: 1rem;
    margin-bottom: 1.5rem;
    border-bottom: 1px solid var(--border);
}
.app-header h1 { margin: 0; font-size: 1.05rem; font-weight: 600; letter-spacing: -0.01em; }
.app-header p { margin: 2px 0 0 0; font-size: 0.82rem; color: var(--muted); }

/* Tombol — default outline (sekunder) */
.stButton > button {
    width: 100%;
    height: 2.6rem;
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 0.9rem;
    border: 1px solid var(--border);
    background: transparent;
    border-radius: var(--radius-sm);
    font-weight: 600;
    transition: border-color 0.15s ease, background 0.15s ease;
}
.stButton > button, .stButton > button p { color: var(--fg) !important; }
.stButton > button:hover { border-color: var(--fg); background: rgba(255,255,255,0.04); }
.stButton > button:focus-visible { outline: 2px solid var(--accent-bright); outline-offset: 2px; }

.section-label {
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    margin: 0 0 0.75rem 0;
}

/* Ringkasan sesi — divider tipis, bukan card */
.summary-row {
    display: flex; gap: 2.5rem; flex-wrap: wrap;
    padding: 1rem 0;
    border-top: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
    margin: 1rem 0;
}
.summary-item .label {
    display: block; font-size: 0.72rem; font-weight: 600; letter-spacing: 0.08em;
    text-transform: uppercase; color: var(--muted); margin-bottom: 0.3rem;
}
.summary-item .value { font-size: 1rem; font-weight: 600; color: var(--fg); }

/* Statistik — angka besar + label, dipisah garis vertikal tipis, bukan card */
.stat-row { display: flex; margin: 1.5rem 0; }
.stat { flex: 1; text-align: center; padding: 0 0.75rem; }
.stat:not(:last-child) { border-right: 1px solid var(--border); }
.stat-value { display: block; font-size: 1.7rem; font-weight: 600; color: var(--fg); }
.stat-label {
    display: block; font-size: 0.7rem; font-weight: 600; letter-spacing: 0.06em;
    text-transform: uppercase; color: var(--muted); margin-top: 0.3rem;
}

[data-testid="stSelectbox"] div[data-baseweb="select"] { cursor: pointer; }
[data-testid="stDateInput"] div[data-baseweb="base-input"] { cursor: pointer; }

/* Emoji "kepala miring" — tidak ada emoji unicode utuh untuk itu, jadi pakai
   yang sudah ada (😵) tapi dimiringkan manual lewat transform. Cuma bisa
   diterapkan di tempat yang HTML-nya saya kontrol langsung (daftar rincian
   kejadian) — di label checkbox Streamlit tidak menerima HTML sama sekali,
   jadi di situ emoji-nya polos saja tanpa rotasi. */
.tilt-icon { display: inline-block; transform: rotate(15deg); }

/* Identitas sidebar dijadikan satu blok, supaya jaraknya tidak melebar
   karena gap antar-elemen bawaan Streamlit. */
.side-user {
    padding-bottom: 0.7rem;
    margin-bottom: 0.7rem;
    border-bottom: 1px solid var(--border);
}
.side-user .name { font-weight: 600; font-size: 0.95rem; }
.side-user .note { font-size: 0.75rem; line-height: 1.45; color: var(--muted); margin-top: 3px; }

/* Baris kejadian — list rata, bukan tumpukan kartu warna-warni */
.event-row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 4px;
    border-bottom: 1px solid var(--border);
}
.event-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.event-label { font-weight: 600; font-size: 0.92rem; }
.event-time  { color: var(--muted); font-size: 0.85rem; }
.event-dur   { margin-left: auto; font-weight: 600; font-size: 0.88rem; font-variant-numeric: tabular-nums; }

@media (max-width: 768px) {
    [data-testid="column"] { width:100%!important; flex:1 1 100%!important; }
    .app-header h1 { font-size: 1rem; }
    .stat-value { font-size: 1.35rem; }
    .summary-row { gap: 1.5rem; }
}
</style>
""", unsafe_allow_html=True)

# ── Helpers ───────────────────────────────────────────────────────────────────
# st.session_state, bukan file di server — lihat catatan di 2_🥱_Deteksi_Kantuk.py
# soal kenapa cookies.pkl (file global di server) adalah bug keamanan serius.
# Di sini set_cookie cuma dipakai untuk logout, jadi selalu bersihkan token
# "ingat saya" di localStorage juga (lihat AUTH_STORAGE_KEY / blok pulihkan
# login di bawah, dan penjelasan lengkapnya di 2_🥱_Deteksi_Kantuk.py).
def set_cookie(username):
    st.session_state["auth_user"] = username
    # Ditunda ke render berikutnya, bukan components.html() langsung di sini —
    # set_cookie() di sini selalu dipanggil tepat sebelum st.experimental_rerun(),
    # yang bisa memotong pengiriman elemen sebelum sempat "nyantol" ke browser.
    # Detail lengkapnya di flush_pending_auth_storage_write() @ 2_🥱_Deteksi_Kantuk.py.
    st.session_state["pending_auth_storage_write"] = "__clear__"

def get_cookie():
    return st.session_state.get("auth_user") or None

def flush_pending_auth_storage_write():
    if "pending_auth_storage_write" in st.session_state:
        pending = st.session_state.pop("pending_auth_storage_write")
        if pending == "__clear__":
            js = f"try {{ localStorage.removeItem('{AUTH_STORAGE_KEY}'); }} catch(e) {{}}"
        else:
            js = f"try {{ localStorage.setItem('{AUTH_STORAGE_KEY}', '{pending}'); }} catch(e) {{}}"
        components.html(f"<script>{js}</script>", height=0)

flush_pending_auth_storage_write()

# ── Pulihkan login dari token di localStorage browser ────────────────────────
# Sama seperti di halaman Deteksi Kantuk: session_state hilang tiap refresh
# browser, jadi tokennya dicek lewat localStorage (per-browser) dan divalidasi
# ke DB sebelum dipercaya.
query_params = st.experimental_get_query_params()
needs_auth_restore = "auth_user" not in st.session_state and not st.session_state.get("auth_restore_done")

if needs_auth_restore:
    if "authtok" in query_params:
        token = query_params["authtok"][0]
        st.session_state["auth_restore_done"] = True
        if token == GUEST_COOKIE_VALUE:
            st.session_state["auth_user"] = GUEST_COOKIE_VALUE
        elif token and token != "-":
            restored_username = get_username_by_token(token)
            if restored_username:
                st.session_state["auth_user"] = restored_username
            else:
                components.html(f"""
                <script>
                try {{ localStorage.removeItem('{AUTH_STORAGE_KEY}'); }} catch(e) {{}}
                </script>
                """, height=0)
        components.html("""
        <script>
        window.parent.history.replaceState(null, "", window.parent.location.pathname);
        </script>
        """, height=0)
        st.experimental_rerun()
    else:
        components.html(f"""
        <script>
        const p = new URLSearchParams(window.parent.location.search);
        if (!p.has('authtok')) {{
            const tok = localStorage.getItem('{AUTH_STORAGE_KEY}') || '-';
            const url = window.parent.location.pathname + '?authtok=' + encodeURIComponent(tok);
            window.parent.history.replaceState(null, '', url);
        }}
        </script>
        """, height=0)
        st.session_state["auth_restore_attempt"] = st.session_state.get("auth_restore_attempt", 0) + 1
        st_autorefresh(interval=200, limit=2, key=f"auth_restore_{st.session_state['auth_restore_attempt']}")
        st.info("Memuat sesi...")
        st.stop()

def fmt_dur(seconds):
    if seconds is None: return "-"
    seconds = int(seconds)
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    if h > 0: return f"{h}j {m}m {s}d"
    if m > 0: return f"{m}m {s}d"
    return f"{s} detik"

EVENT_LABEL = {
    "eye_close": "😴 Mata Tertutup",
    "yawn": "🥱 Menguap",
    "head_tilt": '<span class="tilt-icon">😵</span> Kepala Miring',
}
EVENT_COLOR = {"eye_close": "var(--primary)", "yawn": "var(--accent)", "head_tilt": "var(--accent-bright)"}

# ── Auth ──────────────────────────────────────────────────────────────────────
user, is_guest = resolve_guest_user(get_cookie())

if not user and not is_guest:
    st.error("⛔ Kamu belum login.")
    st.markdown("Silakan login terlebih dahulu di halaman **Deteksi Kantuk**, atau masuk sebagai Tamu.")
    st.stop()

display_name = user or "Tamu"

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    if is_guest:
        side_note = (
            "Kamu masuk sebagai Tamu — riwayat cuma disimpan di browser ini "
            "(maksimal 10 sesi terakhir) dan bisa hilang kalau cache atau data "
            "browser dibersihkan. Daftar akun supaya riwayatmu tersimpan aman."
        )
    else:
        side_note = "Riwayat kamu tersimpan di akun ini."

    st.markdown(f"""
    <div class="side-user">
        <div class="name">{display_name}</div>
        <div class="note">{side_note}</div>
    </div>
    """, unsafe_allow_html=True)

    if st.button("Keluar"):
        if not is_guest:
            clear_session_token(user)
        set_cookie("")
        st.experimental_rerun()

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="app-header">
    <div>
        <h1>Histori Berkendara</h1>
        <p>Halo {display_name}, berikut riwayat sesi berkendara kamu.</p>
    </div>
</div>
""", unsafe_allow_html=True)

events_by_session = None
if is_guest:
    query_params = st.experimental_get_query_params()
    # Kalau halaman utama baru saja menyimpan sesi tamu, data yang nyangkut di
    # URL sudah basi — paksa ambil ulang dari localStorage.
    need_fresh = st.session_state.get("guest_history_dirty", False)
    if "gdata" in query_params and not need_fresh:
        try:
            guest_raw = json.loads(unquote(query_params["gdata"][0]))
        except Exception:
            guest_raw = []
        sessions = [
            (s.get("id"), s.get("start_time"), s.get("end_time"), s.get("duration"),
             s.get("total_eye_close", 0), s.get("total_yawn", 0), s.get("total_head_tilt", 0))
            for s in guest_raw
        ]
        events_by_session = {
            s.get("id"): [(e["event_type"], e["event_time"], e["duration"]) for e in s.get("events", [])]
            for s in guest_raw
        }
    else:
        # Selalu tulis ulang, tanpa cek "kalau belum ada di URL" — justru cek
        # itu yang bikin sesi tamu yang baru selesai tidak pernah kelihatan
        # sampai halaman di-refresh manual.
        components.html(f"""
        <script>
        const raw = localStorage.getItem('{GUEST_STORAGE_KEY}') || '[]';
        const url = window.parent.location.pathname + '?gdata=' + encodeURIComponent(raw);
        window.parent.history.replaceState(null, '', url);
        </script>
        """, height=0)
        st.session_state["guest_history_dirty"] = False
        # Key diberi suffix unik per kunjungan halaman ini, karena limit
        # st_autorefresh terikat ke key seumur sesi — key tetap akan "habis"
        # dan tidak jalan lagi saat halaman ini dibuka ulang.
        st.session_state["guest_fetch_attempt"] = st.session_state.get("guest_fetch_attempt", 0) + 1
        st_autorefresh(interval=300, limit=2, key=f"guest_data_fetch_{st.session_state['guest_fetch_attempt']}")
        st.info("Memuat histori tamu...")
        st.stop()
else:
    sessions = get_sessions_by_username(user)

if not sessions:
    st.info("Belum ada histori berkendara. Mulai sesi dari halaman Deteksi Kantuk.")
    st.stop()

# ── Pilih sesi ────────────────────────────────────────────────────────────
def session_label(row):
    sid, start, end, dur, eye, yawn, tilt = row
    total = (eye or 0) + (yawn or 0) + (tilt or 0)
    try:
        date_str = datetime.strptime(start, "%Y-%m-%d %H:%M:%S").strftime("%d-%m-%Y")
    except Exception:
        date_str = start
    return f"🚗  {date_str}  |  {total} kejadian"

all_dates = []
for row in sessions:
    try:
        all_dates.append(datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S").date())
    except:
        pass

min_date = min(all_dates) if all_dates else date.today()
max_date = max(all_dates) if all_dates else date.today()

f1, f2 = st.columns(2)
with f1:
    date_from = st.date_input("📅 Dari tanggal:", value=min_date, min_value=min_date, max_value=max_date)
with f2:
    date_to   = st.date_input("📅 Sampai tanggal:", value=max_date, min_value=min_date, max_value=max_date)

# Filter sesi berdasarkan range
filtered_sessions = []
for row in sessions:
    try:
        row_date = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S").date()
        if date_from <= row_date <= date_to:
            filtered_sessions.append(row)
    except:
        pass

if not filtered_sessions:
    st.warning("Tidak ada sesi pada rentang tanggal yang dipilih.")
    st.stop()

st.caption(f"Menampilkan **{len(filtered_sessions)}** dari **{len(sessions)}** sesi")

selected_id = st.selectbox(
    "Pilih sesi berkendara:",
    options=[r[0] for r in filtered_sessions],
    format_func=lambda x: session_label(next(r for r in filtered_sessions if r[0] == x))
)

selected = next(r for r in sessions if r[0] == selected_id)
sid, start_time, end_time, duration, total_eye, total_yawn, total_tilt = selected

def fmt_dt(dt_str):
    if not dt_str:
        return None
    try:
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").strftime("%d-%m-%Y %H:%M:%S")
    except Exception:
        return dt_str

# ── Ringkasan sesi ────────────────────────────────────────────────────────────
st.markdown("### Ringkasan Sesi")

st.markdown(f"""
<div class="summary-row">
    <div class="summary-item"><span class="label">Mulai</span><span class="value mono">{fmt_dt(start_time)}</span></div>
    <div class="summary-item"><span class="label">Selesai</span><span class="value mono">{fmt_dt(end_time) or "—"}</span></div>
    <div class="summary-item"><span class="label">Total Durasi</span><span class="value mono">{fmt_dur(duration)}</span></div>
</div>
""", unsafe_allow_html=True)

st.markdown(f"""
<div class="stat-row">
    <div class="stat"><span class="stat-value mono">{total_eye or 0}</span><span class="stat-label">Mata Tertutup</span></div>
    <div class="stat"><span class="stat-value mono">{total_yawn or 0}</span><span class="stat-label">Menguap</span></div>
    <div class="stat"><span class="stat-value mono">{total_tilt or 0}</span><span class="stat-label">Kepala Miring</span></div>
</div>
""", unsafe_allow_html=True)

st.markdown("---")

# ── Detail event ──────────────────────────────────────────────────────────────
events = events_by_session[selected_id] if is_guest else get_events_by_session(selected_id)

if not events:
    st.info("Tidak ada kejadian kantuk yang tercatat pada sesi ini.")
    st.stop()

st.markdown("### Detail Kejadian Kantuk")

# Filter checkbox
fc1, fc2, fc3, _ = st.columns(4)
show_eye  = fc1.checkbox("😴 Mata Tertutup", value=True)
show_yawn = fc2.checkbox("🥱 Menguap",       value=True)
show_tilt = fc3.checkbox("😵 Kepala Miring", value=True)

filtered = [
    e for e in events
    if (e[0] == "eye_close" and show_eye)
    or (e[0] == "yawn"      and show_yawn)
    or (e[0] == "head_tilt" and show_tilt)
]

st.caption(f"Menampilkan **{len(filtered)}** dari **{len(events)}** kejadian")

for event_type, event_time, dur in filtered:
    label = EVENT_LABEL.get(event_type, event_type)
    color = EVENT_COLOR.get(event_type, "#8b8b92")
    try:
        time_str = datetime.strptime(event_time, "%Y-%m-%d %H:%M:%S").strftime("%H:%M:%S")
    except Exception:
        time_str = event_time
    st.markdown(f"""
    <div class="event-row">
        <span class="event-dot" style="background:{color};"></span>
        <span class="event-label">{label}</span>
        <span class="event-time mono">Mulai {time_str}</span>
        <span class="event-dur mono">{dur:.1f}s</span>
    </div>
    """, unsafe_allow_html=True)