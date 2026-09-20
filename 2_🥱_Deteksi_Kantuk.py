import os
import re
import av
import random
import string
import threading
import traceback
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import streamlit as st
import streamlit_nested_layout
from streamlit_webrtc import VideoHTMLAttributes, webrtc_streamer
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh

from audio_handling import AudioFrameHandler
from deteksi_kantuk import VideoFrameHandler, set_location_cache
from database import (
    create_db, add_user, check_username_exists, check_login,
    start_session, end_session, save_otp, verify_otp, get_user_email,
    resolve_guest_user, compute_session_totals, GUEST_COOKIE_VALUE, GUEST_STORAGE_KEY,
    now_wib, set_session_token, get_username_by_token, clear_session_token,
    AUTH_STORAGE_KEY,
)
from hashlib import sha256
from decouple import config
import requests
import time
import json
from datetime import datetime

create_db()

# ── Konstanta email pengirim ──────────────────────────────────────────────────
EMAIL_SENDER   = "deteksikantuk@gmail.com"
EMAIL_PASSWORD = config('EMAIL_PASSWORD')

# Berapa detik kamera harus benar-benar mati sebelum sesi dianggap selesai.
# Jangan dibikin terlalu pendek: kalau koneksi WebRTC sempat goyang sebentar,
# sesi keburu dianggap selesai padahal user tidak menekan STOP.
CAMERA_OFF_GRACE = 3.0

# ── STUN/TURN ─────────────────────────────────────────────────────────────────
# Di localhost, STUN saja cukup karena browser dan server ada di mesin yang
# sama. Begitu di-deploy ke Streamlit Community Cloud, servernya ada di balik
# proxy/firewall yang memblokir jalur WebRTC langsung: negosiasi ICE mentok di
# status `signalling` dan tidak pernah sampai `playing`, jadi videonya kosong
# terus walau tombolnya sudah berubah jadi STOP. TURN dipakai sebagai perantara
# yang melewatkan media saat jalur langsung tidak bisa terbentuk.
#
# Kredensial dibaca dari Secrets (Streamlit Cloud) / .env (lokal), tidak pernah
# masuk git. Kalau belum diisi, otomatis balik ke STUN-only supaya development
# di lokal tetap jalan tanpa perlu setup apa pun.
METERED_APP_NAME = config('METERED_APP_NAME', default='')
METERED_API_KEY  = config('METERED_API_KEY', default='')

STUN_ONLY = [{"urls": ["stun:stun.l.google.com:19302"]}]

def build_ice_servers():
    """Ambil daftar server ICE (STUN + TURN) dari Metered.

    Kredensial TURN-nya berumur pendek dan diambil lewat API, jadi yang
    disimpan di Secrets cuma nama app + API key. Hasilnya di-cache karena
    halaman ini di-rerun tiap detik saat monitoring aktif — tidak perlu
    menembak API tiap rerun.
    """
    if not (METERED_APP_NAME and METERED_API_KEY):
        return STUN_ONLY

    cached = st.session_state.get("ice_servers_cache")
    if cached and time.time() < cached["expires"]:
        return cached["servers"]

    try:
        resp = requests.get(
            f"https://{METERED_APP_NAME}.metered.live/api/v1/turn/credentials",
            params={"apiKey": METERED_API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        servers = resp.json()
    except Exception as e:
        # Jangan sampai app mati cuma karena TURN tidak terjangkau — di lokal
        # STUN saja memang sudah cukup.
        print(f"Gagal ambil kredensial TURN: {e}")
        return STUN_ONLY

    st.session_state["ice_servers_cache"] = {"servers": servers, "expires": time.time() + 1800}
    return servers

# ── Sesi login ────────────────────────────────────────────────────────────────
# PENTING: dulu ini disimpan lewat file cookies.pkl di server — itu BUKAN
# cookie per-browser, itu satu file GLOBAL yang dipakai bersama oleh SEMUA
# pengunjung. Siapa pun yang login akan menimpa file itu, dan pengunjung lain
# yang membuka app yang sama otomatis "ikut login" sebagai orang terakhir yang
# menimpanya — bug keamanan serius di deployment multi-pengguna (Streamlit
# Cloud). st.session_state benar-benar terpisah per koneksi browser/tab, jadi
# login satu orang tidak lagi bisa bocor ke orang lain.
#
# Supaya tetap login walau tab di-refresh (session_state doang hilang begitu
# WebSocket-nya putus), kita SIMPAN JUGA sebuah token ke localStorage browser
# yang bersangkutan lewat JS. Ini beda dari bug cookies.pkl di atas: localStorage
# sudah per-browser/per-origin dari sananya (tidak pernah dibagi antar
# pengunjung), dan isinya token acak yang divalidasi ke DB (lihat
# get_username_by_token di blok "Pulihkan login" di bawah) — bukan username
# mentah, jadi tidak bisa dipalsukan cuma dengan mengetik nama orang lain di
# DevTools.
def set_cookie(username):
    st.session_state["auth_user"] = username
    if username == GUEST_COOKIE_VALUE:
        stored_value = GUEST_COOKIE_VALUE
    elif username:
        stored_value = set_session_token(username)
    else:
        stored_value = None

    # Penulisan ke localStorage DITUNDA ke render berikutnya, bukan
    # components.html() langsung di sini — set_cookie() selalu dipanggil tepat
    # sebelum st.experimental_rerun(), dan rerun itu bisa memotong pengiriman
    # elemen yang baru saja di-queue sebelum sempat "nyantol" ke browser (kasus
    # yang sama persis dengan toast "Sesi tersimpan" yang sempat hilang).
    # Tanpa ini, token akun asli bisa gagal menimpa nilai lama (mis. "__guest__"
    # dari percobaan Mode Tamu sebelumnya) di localStorage.
    st.session_state["pending_auth_storage_write"] = stored_value if stored_value else "__clear__"

def get_cookie():
    return st.session_state.get("auth_user") or None

def flush_pending_auth_storage_write():
    """Jalankan penulisan localStorage yang ditunda oleh set_cookie() di
    render sebelumnya. Harus dipanggil di awal tiap render, sebelum ada
    st.stop() yang bisa menghalangi ini kejalan."""
    if "pending_auth_storage_write" in st.session_state:
        pending = st.session_state.pop("pending_auth_storage_write")
        if pending == "__clear__":
            js = f"try {{ localStorage.removeItem('{AUTH_STORAGE_KEY}'); }} catch(e) {{}}"
        else:
            js = f"try {{ localStorage.setItem('{AUTH_STORAGE_KEY}', '{pending}'); }} catch(e) {{}}"
        components.html(f"<script>{js}</script>", height=0)

def save_guest_session(session_id, start_time_str, events):
    """Simpan satu sesi tamu ke localStorage browser (maksimal 10 sesi terbaru)."""
    end_time_str = now_wib().strftime('%Y-%m-%d %H:%M:%S')
    try:
        start_dt = datetime.strptime(start_time_str, '%Y-%m-%d %H:%M:%S')
        end_dt   = datetime.strptime(end_time_str, '%Y-%m-%d %H:%M:%S')
        duration = int((end_dt - start_dt).total_seconds())
    except Exception:
        duration = None

    total_eye_close, total_yawn, total_head_tilt = compute_session_totals(events)

    session_data = {
        "id": session_id,
        "start_time": start_time_str,
        "end_time": end_time_str,
        "duration": duration,
        "total_eye_close": total_eye_close,
        "total_yawn": total_yawn,
        "total_head_tilt": total_head_tilt,
        "events": events,
    }
    # Tandai supaya halaman Histori tahu isi localStorage sudah berubah dan
    # harus mengambil ulang, bukan memakai data yang terlanjur nyangkut di URL.
    st.session_state["guest_history_dirty"] = True

    payload = json.dumps(session_data).replace("</", "<\\/")
    components.html(f"""
    <script>
    try {{
        let arr = JSON.parse(localStorage.getItem('{GUEST_STORAGE_KEY}') || '[]');
        arr.unshift({payload});
        if (arr.length > 10) arr = arr.slice(0, 10);
        localStorage.setItem('{GUEST_STORAGE_KEY}', JSON.stringify(arr));
    }} catch (e) {{ console.error('Gagal simpan histori tamu:', e); }}
    </script>
    """, height=0)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def is_valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match(value))

# ── Kirim OTP ─────────────────────────────────────────────────────────────────
def generate_otp(length=6):
    return ''.join(random.choices(string.digits, k=length))

def send_otp_email(to_email: str, otp_code: str, username: str):
    try:
        msg = MIMEMultipart()
        msg['From']    = EMAIL_SENDER
        msg['To']      = to_email
        msg['Subject'] = "🔐 Kode Verifikasi Sistem Pendeteksi Kantuk"
        body = (
            f"Seseorang dengan username '{username}' mendaftarkan email ini sebagai "
            f"kontak darurat pada Sistem Pendeteksi Kantuk.\n\n"
            f"Kode verifikasi kamu:\n\n"
            f"{otp_code}\n\n"
            f"Kode ini berlaku selama 10 menit.\n"
            f"Jika kamu tidak merasa mendaftar, abaikan email ini.\n\n"
            f"— Sistem Pendeteksi Kantuk"
        )
        msg.attach(MIMEText(body, 'plain'))
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"Gagal kirim OTP: {e}")
        return False

# ── CSS ────────────────────────────────────────────────────────────────────────
# Satu arah desain: panel instrumen malam hari — netral gelap + satu warna
# aksen (amber, kayak lampu peringatan di dashboard mobil), bukan warna-warni.
BG_COLOR = "#123A63"

MOBILE_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&display=swap');

:root {
    --bg:         __BG_COLOR__;
    --surface:    #17477A;
    --border:     rgba(255,255,255,0.10);
    --fg:         #f5f4f0;
    --muted:      #9FB3C8;
    --accent:     #ff8a1e;
    --accent-fg:  #16110a;
    --radius-sm:  6px;
    --radius-md:  10px;
}

html, body, [class*="css"] { font-family: 'Inter', sans-serif; color: var(--fg); }
h1, h2, h3 { font-family: 'Space Grotesk', sans-serif; }

/* Buang chrome bawaan Streamlit */
#MainMenu, footer, [data-testid="stToolbar"] { visibility: hidden; }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stDecoration"] { display: none; }

/* Background: warna dasar + dot-grid halus dan sorotan lembut di tengah atas.
   Statis (tidak animasi) dan kontras titiknya sangat rendah supaya tidak
   bikin mata lelah, tapi cukup untuk memberi kesan panel instrumen dan
   menarik pandangan ke area kamera di tengah. */
[data-testid="stAppViewContainer"] {
    background-color: var(--bg);
    background-image:
        radial-gradient(ellipse at 50% 0%, rgba(255,255,255,0.055), transparent 62%),
        radial-gradient(rgba(255,255,255,0.05) 1px, transparent 1px);
    background-size: 100% 100%, 22px 22px;
    background-attachment: fixed;
}
[data-testid="stAppViewContainer"] > .main > div {
    padding: 1.5rem 2rem 3rem 2rem !important;
    max-width: 860px;
    margin: 0 auto;
}
[data-testid="stSidebar"] { background: var(--surface); border-right: 1px solid var(--border); }

/* Identitas sidebar dijadikan satu blok, supaya jaraknya tidak melebar
   karena gap antar-elemen bawaan Streamlit. */
.side-user {
    font-weight: 600;
    font-size: 0.95rem;
    padding-bottom: 0.7rem;
    margin-bottom: 0.7rem;
    border-bottom: 1px solid var(--border);
}

/* ── Header ───────────────────────────────────────────────── */
.app-header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    padding-bottom: 1rem;
    margin-bottom: 1.75rem;
    border-bottom: 1px solid var(--border);
}
.app-header h1 {
    margin: 0;
    font-size: 1.3rem;
    font-weight: 700;
    letter-spacing: -0.01em;
}
.app-header p { margin: 2px 0 0 0; font-size: 0.85rem; color: var(--muted); }
.status-dot {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 0.78rem; font-weight: 600; color: var(--accent);
    white-space: nowrap;
}
.status-dot::before {
    content: ""; width: 7px; height: 7px; border-radius: 50%;
    background: var(--accent);
}

/* ── Section — pengganti "card" berat, cukup garis & label ─── */
.section-label {
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    margin: 0 0 0.75rem 0;
}
/* ── Tombol — default: outline (aksi sekunder) ──────────────── */
.stButton > button {
    width: 100%;
    height: 2.6rem;
    font-family: 'Inter', sans-serif;
    font-size: 0.9rem;
    font-weight: 600;
    white-space: nowrap;
    border-radius: var(--radius-sm);
    cursor: pointer;
    background: transparent;
    border: 1px solid var(--border);
    transition: border-color 0.15s ease, background 0.15s ease;
}
.stButton > button, .stButton > button p { color: var(--fg) !important; }
.stButton > button:hover { border-color: var(--fg); background: rgba(255,255,255,0.04); }
.stButton > button:disabled { opacity: 0.45; cursor: not-allowed; }

/* Tombol utama — ditandai marker tak-terlihat sebelum tombolnya */
.element-container:has(.btn-primary-marker) + .element-container .stButton > button {
    background: var(--accent);
    border-color: var(--accent);
}
.element-container:has(.btn-primary-marker) + .element-container .stButton > button,
.element-container:has(.btn-primary-marker) + .element-container .stButton > button p {
    color: var(--accent-fg) !important;
}
.element-container:has(.btn-primary-marker) + .element-container .stButton > button:hover {
    background: #ffa347;
    border-color: #ffa347;
}

/* Tombol bergaya teks bergaris bawah (mis. "Ganti email", "Kirim ulang") */
.element-container:has(.btn-link-marker) + .element-container .stButton,
.element-container:has(.btn-link-marker-right) + .element-container .stButton {
    width: auto;
    display: inline-block;
}
.element-container:has(.btn-link-marker) + .element-container .stButton > button,
.element-container:has(.btn-link-marker-right) + .element-container .stButton > button {
    width: auto;
    height: auto;
    padding: 0;
    background: transparent !important;
    border: none;
    color: var(--muted) !important;
    text-decoration: underline;
    text-underline-offset: 2px;
    font-weight: 500;
    font-size: 0.85rem;
    white-space: nowrap;
}
.element-container:has(.btn-link-marker) + .element-container .stButton > button p,
.element-container:has(.btn-link-marker-right) + .element-container .stButton > button p {
    color: var(--muted) !important;
}
.element-container:has(.btn-link-marker) + .element-container .stButton > button:hover,
.element-container:has(.btn-link-marker-right) + .element-container .stButton > button:hover {
    text-decoration: none;
    background: transparent !important;
    border: none;
    color: var(--fg) !important;
}
.element-container:has(.btn-link-marker) + .element-container .stButton > button:hover p,
.element-container:has(.btn-link-marker-right) + .element-container .stButton > button:hover p {
    color: var(--fg) !important;
}
.element-container:has(.btn-link-marker-right) + .element-container { text-align: right; }

/* Rapatkan gap sebelum tombol link (mis. "Ganti email" nempel ke teks di atasnya) */
.element-container:has(.btn-link-marker) {
    margin-top: -10px;
    margin-bottom: -14px;
}

/* ── Input field ─────────────────────────────────────────── */
input[type="text"], input[type="password"], input[type="number"],
[data-testid="stTextInput"] input {
    background: transparent !important;
    color: var(--fg) !important;
    border: 1px solid var(--border) !important;
    font-size: 1rem !important;
    height: 2.6rem !important;
    border-radius: var(--radius-sm) !important;
}
[data-testid="stTextInput"] input:focus { border-color: var(--accent) !important; }
[data-testid="stTextInput"] input::placeholder { color: var(--muted) !important; }

/* Validasi field inline ala Instagram — marker sebelum field menandai kotak
   yang bermasalah dengan border merah, teksnya muncul tepat di bawah field. */
.element-container:has(.field-error-marker) + .element-container [data-testid="stTextInput"] input {
    border-color: #ff5c5c !important;
}
.field-error-text {
    margin: -12px 0 10px 0;
    font-size: 0.8rem;
    color: #ff5c5c;
}

/* Ikon show/hide password — center-kan vertikal di dalam kotak input */
[data-testid="stTextInput"] div[data-baseweb="base-input"] {
    display: flex;
    align-items: center;
}
div[data-baseweb="input"].st-ck, div[data-baseweb="input"].st-cm {
    padding-right: 0px !important;
}
[data-testid="stTextInput"] button {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
}

/* ── Slider ───────────────────────────────────────────────── */
[data-baseweb="slider"] [role="slider"] { background: var(--accent) !important; }

/* ── Auth (Masuk / Daftar / OTP) ─────────────────────────── */
.auth-title {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 1.5rem;
    font-weight: 700;
    margin-bottom: 0.2rem;
    text-align: center;
    color: var(--fg);
}
.auth-subtitle {
    font-size: 0.85rem;
    color: var(--muted);
    text-align: center;
    margin-bottom: 1.75rem;
}
/* ── Video kamera ────────────────────────────────────────── */
video {
    border-radius: var(--radius-md);
    width: 100% !important;
    height: auto !important;
    border: 1px solid var(--border);
}

/* ── Alert box ───────────────────────────────────────────── */
[data-testid="stAlert"] { border-radius: var(--radius-sm); }

/* ── Mobile ──────────────────────────────────────────────── */
@media (max-width: 768px) {
    [data-testid="stAppViewContainer"] > .main > div {
        padding: 1rem 1rem 2rem 1rem !important;
    }
    [data-testid="column"] {
        width: 100% !important;
        flex: 1 1 100% !important;
        min-width: 100% !important;
    }
    .stButton > button { height: 3rem; font-size: 1rem; }
    input[type="text"], input[type="password"] {
        height: 3rem !important;
        font-size: 1rem !important;
    }
    .auth-title { font-size: 1.3rem; }
    .app-header h1 { font-size: 1.1rem; }
}
</style>
""".replace("__BG_COLOR__", BG_COLOR)

# ─── App ─────────────────────────────────────────────────────────────────────
query_params = st.experimental_get_query_params()
if "loc" in query_params:
    set_location_cache(query_params["loc"][0])

# Sesi baru (abis refresh / tab baru) belum tahu siapa usernya sampai token di
# localStorage browser ini sempat dibaca — lihat blok "Pulihkan login" di bawah.
needs_auth_restore = "auth_user" not in st.session_state and not st.session_state.get("auth_restore_done")

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Sistem Pendeteksi Kantuk",
    page_icon="https://cdn-icons-png.flaticon.com/512/1464/1464723.png",
    layout="wide",
    initial_sidebar_state="collapsed" if needs_auth_restore else ("expanded" if get_cookie() else "collapsed"),
)
st.markdown(MOBILE_CSS, unsafe_allow_html=True)
flush_pending_auth_storage_write()

# ── Pulihkan login dari token di localStorage browser ────────────────────────
# Streamlit tidak menyimpan session_state lewat refresh browser (WebSocket
# lama putus, sesi baru dibuat) — makanya dulu selalu balik ke halaman login.
# Di sini kita cek localStorage LEWAT JS (bukan file di server!), kirim
# hasilnya balik lewat query param URL, lalu divalidasi ke DB. Coba SEKALI
# saja per sesi (auth_restore_done) supaya tidak looping kalau localStorage
# kosong/invalid.
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
                # Token basi/tidak valid — bersihkan supaya tidak diulang tiap kunjungan.
                components.html(f"""
                <script>
                try {{ localStorage.removeItem('{AUTH_STORAGE_KEY}'); }} catch(e) {{}}
                </script>
                """, height=0)
        # Bersihkan token dari address bar — kalau nyangkut kelihatan di URL,
        # bisa ke-screenshot/ke-share orang lain dan dipakai login tanpa password.
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
        # Key diberi suffix unik per percobaan — limit st_autorefresh terikat
        # ke key seumur sesi, jadi tanpa ini pemulihan cuma jalan sekali.
        st.session_state["auth_restore_attempt"] = st.session_state.get("auth_restore_attempt", 0) + 1
        st_autorefresh(interval=200, limit=2, key=f"auth_restore_{st.session_state['auth_restore_attempt']}")
        st.caption("Memuat sesi...")
        st.stop()

user, is_guest = resolve_guest_user(get_cookie())
logged_in = bool(user or is_guest)

# ══════════════════════════════════════════════════════════════════════════════
# SUDAH LOGIN / TAMU
# ══════════════════════════════════════════════════════════════════════════════
if logged_in:
    display_name = user or "Tamu"
    alarm_file_path = os.path.join("audio", "wake_up.wav")

    # Tentukan lebih dulu apakah ada email tujuan alarm — dipakai untuk
    # video_handler.email_recipients di bawah, DAN untuk memutuskan perlu
    # tidaknya minta izin lokasi (lokasi cuma dipakai di body email alarm,
    # jadi tamu / akun tanpa email terdaftar tidak perlu ditanya lokasi).
    if is_guest:
        recipient_emails = []
    else:
        # Cache per user, bukan query DB tiap rerun — halaman ini di-autorefresh
        # tiap 1 detik selama monitoring aktif.
        if st.session_state.get("email_recipient_for") != user:
            st.session_state["email_recipient_for"] = user
            recipient_email = get_user_email(user)
            st.session_state["email_recipient_cached"] = [recipient_email] if recipient_email else []
        recipient_emails = st.session_state["email_recipient_cached"]

    # GPS realtime — cuma diminta kalau memang ada tujuan emailnya
    if recipient_emails:
        components.html("""
        <script>
        if (navigator.geolocation) {
            navigator.geolocation.watchPosition(
                function(pos) {
                    const lat = pos.coords.latitude;
                    const lon = pos.coords.longitude;
                    const link = "https://www.google.com/maps?q=" + lat + "," + lon;
                    const newUrl = window.parent.location.href.split("?")[0] + "?loc=" + encodeURIComponent(link);
                    window.parent.history.replaceState(null, "", newUrl);
                },
                function(err) { console.error("Gagal ambil lokasi:", err); },
                { enableHighAccuracy: true, maximumAge: 0 }
            );
        }
        </script>
        """, height=0)

    # Cegah layar mati saat deteksi berjalan
    components.html("""
    <script>
    let wakeLock = null;
    async function requestWakeLock() {
        try {
            wakeLock = await navigator.wakeLock.request("screen");
        } catch (err) {
            console.error("Wake lock gagal:", err);
        }
    }
    requestWakeLock();
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") requestWakeLock();
    });
    </script>
    """, height=0)

    with st.sidebar:
        st.markdown(f'<div class="side-user">{display_name}</div>', unsafe_allow_html=True)

        if st.button("Keluar"):
            # Auto-save sesi aktif sebelum logout
            if st.session_state.get("is_monitoring") and st.session_state.get("session_id"):
                pending = video_handler.pending_events if "video_handler" in st.session_state else []
                if is_guest:
                    save_guest_session(st.session_state["session_id"], st.session_state.get("guest_session_start"), pending)
                else:
                    end_session(st.session_state["session_id"], pending)
            if not is_guest:
                clear_session_token(user)
            set_cookie("")
            st.experimental_rerun()

    st.markdown("""
    <div class="app-header">
        <div>
            <h1>Pendeteksi Kantuk</h1>
            <p>Pada pengendara roda empat</p>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Ambang batas sensor — 1 kolom di mobile, 2 kolom di desktop
    st.markdown('<p class="section-label">Sensitivitas Deteksi</p>', unsafe_allow_html=True)
    s1, s2 = st.columns(2)
    with s1:
        WAIT_TIME = st.slider("Waktu alarm menyala (detik):", 0.0, 5.0, 1.0, 0.25,
            help="Berapa lama kondisi kantuk berlangsung sebelum alarm berbunyi. "
                 "Berlaku untuk semua jenis deteksi. "
                 "Referensi: Wierwille & Ellsworth (1994) menyarankan 1–2 detik.")
        MAR_THRESH = st.slider("Ambang batas mulut (menguap):", 0.3, 1.0, 0.6, 0.01,
            help="Nilai MAR di atas threshold = mulut terbuka lebar = menguap. "
                 "Referensi: Mandal et al. (2017) & Vural et al. (2007), default 0.6.")
    with s2:
        EAR_THRESH = st.slider("Ambang batas mata (tertutup):", 0.0, 0.4, 0.23, 0.01,
            help="Nilai EAR di bawah threshold = mata tertutup = mengantuk. "
                 "Referensi: Soukupová & Čech (2016) menyarankan 0.2–0.25.")
        TILT_THRESH = st.slider("Ambang batas kemiringan kepala (°):", 5.0, 45.0, 20.0, 1.0,
            help="Sudut kemiringan kepala dari posisi tegak. "
                 "Referensi: Sahayadhas et al. (2012) — > 15° kantuk ringan, > 25° kantuk berat.")

    thresholds = {
        "EAR_THRESH": EAR_THRESH, "WAIT_TIME": WAIT_TIME,
        "MAR_THRESH": MAR_THRESH, "TILT_THRESH": TILT_THRESH,
    }

    if "session_id"    not in st.session_state: st.session_state["session_id"]    = None
    if "is_monitoring" not in st.session_state: st.session_state["is_monitoring"] = False
    if "video_handler" not in st.session_state: st.session_state["video_handler"] = VideoFrameHandler()

    video_handler = st.session_state["video_handler"]
    video_handler.email_recipients = recipient_emails
    audio_handler = AudioFrameHandler(sound_file_path=alarm_file_path)
    lock = threading.Lock()
    shared_state = {"play_alarm": False}

    def video_frame_callback(frame: av.VideoFrame):
        try:
            img = frame.to_ndarray(format="bgr24")
            img, play_alarm = video_handler.process(img, thresholds)
            with lock:
                shared_state["play_alarm"] = play_alarm
            return av.VideoFrame.from_ndarray(img, format="bgr24")
        except Exception:
            # Callback ini jalan di thread kamera. Kalau error-nya dibiarkan
            # lolos, aiortc menelannya diam-diam: frame berhenti dikirim,
            # video jadi kosong, tanpa pesan apa pun. Dicetak ke log supaya
            # bisa dilacak, ditandai supaya main thread bisa memberi tahu user,
            # dan frame aslinya tetap dikembalikan supaya koneksinya tidak
            # ikut mati.
            video_handler.last_error = True
            print("Error di video_frame_callback:\n" + traceback.format_exc())
            return frame

    def audio_frame_callback(frame: av.AudioFrame):
        with lock:
            play_alarm = shared_state["play_alarm"]
        return audio_handler.process(frame, play_sound=play_alarm)

    # Key sengaja TETAP, bukan dinamis. Sempat dibuat berubah tiap sesi selesai
    # untuk mengakali START yang kadang macet, tapi itu menyisakan instance
    # komponen lama tiap siklus STOP→START — dugaan kuat penyebab kamera makin
    # sering gagal nyambung setelah dipakai beberapa kali.
    ctx = webrtc_streamer(
        key="drowsiness-detection",
        video_frame_callback=video_frame_callback,
        audio_frame_callback=audio_frame_callback,
        rtc_configuration={"iceServers": build_ice_servers()},
        media_stream_constraints={"video": {"height": {"ideal": 480}}, "audio": True},
        video_html_attrs=VideoHTMLAttributes(
            autoPlay=True, controls=False, muted=False,
            style={"width": "100%", "height": "100%", "objectFit": "cover", "borderRadius": "10px"},
        ),
    )

    # ── Auto-start saat kamera ON, auto-save saat kamera OFF ─────────────────
    # `playing` bisa False sesaat walau kamera sebenarnya masih jalan (WebRTC
    # lagi negosiasi / reconnect). Selama masih `signalling`, kamera dianggap
    # hidup — ini kriteria yang sama dipakai streamlit_webrtc sendiri sebelum
    # membunuh worker-nya. Tanpa ini sesi bisa tertutup sendiri di tengah jalan.
    cam_playing    = bool(ctx.state.playing) if ctx and ctx.state else False
    cam_signalling = bool(ctx.state.signalling) if ctx and ctx.state else False
    camera_active  = cam_playing or cam_signalling

    # Diukur pakai jam, bukan jumlah rerun: rerun bisa terjadi beruntun dalam
    # hitungan milidetik (autorefresh + perubahan nilai komponen barengan),
    # jadi "sekian kali pengecekan" gampang habis di dalam satu gangguan sesaat.
    now = time.time()
    if camera_active:
        st.session_state["camera_off_since"] = None
    elif st.session_state.get("camera_off_since") is None:
        st.session_state["camera_off_since"] = now

    off_since   = st.session_state.get("camera_off_since")
    camera_off_for = (now - off_since) if off_since else 0.0

    if cam_playing and not st.session_state["is_monitoring"]:
        # Kamera baru nyala → mulai sesi otomatis
        if is_guest:
            sid = f"g_{int(time.time() * 1000)}"
            st.session_state["guest_session_start"] = now_wib().strftime('%Y-%m-%d %H:%M:%S')
        else:
            sid = start_session(user)
        st.session_state["session_id"]    = sid
        st.session_state["is_monitoring"] = True
        video_handler.pending_events.clear()
        video_handler._eye_event_start  = None
        video_handler._yawn_event_start = None
        video_handler._tilt_event_start = None

    # Sesi baru ditutup kalau kamera benar-benar mati selama CAMERA_OFF_GRACE
    # detik berturut-turut, supaya gangguan sesaat tidak salah dibaca sebagai
    # user menekan STOP.
    elif st.session_state["is_monitoring"] and camera_off_for >= CAMERA_OFF_GRACE:
        if st.session_state["session_id"] is not None:
            if is_guest:
                save_guest_session(st.session_state["session_id"], st.session_state.get("guest_session_start"), video_handler.pending_events)
            else:
                end_session(st.session_state["session_id"], video_handler.pending_events)
            total = len(video_handler.pending_events)
            st.session_state["is_monitoring"] = False
            st.session_state["session_id"]    = None
            st.session_state["camera_off_since"] = None
            video_handler.pending_events.clear()
            # JANGAN panggil st.experimental_rerun() di sini. Untuk mode tamu,
            # save_guest_session() di atas menulis ke localStorage lewat
            # components.html, dan rerun akan memotong render sebelum elemen
            # itu sampai ke browser — sesinya jadi tidak pernah tersimpan.
            st.success(f"Sesi tersimpan! {total} kejadian kantuk tercatat.")

    # ── Info status ───────────────────────────────────────────────────────────
    # Rerun berkala selama kamera/sesi hidup: event kantuk masuk dari thread
    # kamera di background, dan transisi kamera nyala/mati tidak selalu memicu
    # rerun sendiri — tanpa ini status kamera baru kebaca saat ada interaksi
    # manual (itu sebabnya START seolah perlu dipencet dua kali).
    if st.session_state["is_monitoring"] or camera_active:
        st_autorefresh(interval=1000, key="live_session_counter")

    if st.session_state["is_monitoring"]:
        st.markdown(
            f'<span class="status-dot">Sesi aktif — {len(video_handler.pending_events)} kejadian tercatat</span>',
            unsafe_allow_html=True,
        )
    elif not camera_active:
        st.caption("Tekan START pada kamera untuk memulai sesi monitoring.")

    if getattr(video_handler, "last_error", None):
        st.warning("Ada gangguan saat memproses frame kamera. Detailnya tercatat di log aplikasi.")

    # DIAGNOSTIK SEMENTARA — memisahkan dua penyebab yang gejalanya mirip di HP:
    # kamera ditolak browser (izin/secure context) vs koneksi WebRTC tidak
    # terbentuk. Hapus setelah ketemu penyebabnya.
    st.caption(f"server · playing={cam_playing} · signalling={cam_signalling}")
    # Sengaja TIDAK memanggil getUserMedia: itu akan merebut kamera dari
    # komponen WebRTC (di HP kamera biasanya cuma bisa dipakai satu pemakai),
    # jadi diagnostiknya sendiri yang akan merusak hal yang sedang diperiksa.
    components.html("""
    <div id="d" style="font:12px system-ui;color:#9FB3C8">memeriksa browser...</div>
    <script>
    const d = document.getElementById('d');
    const p = ['https=' + window.isSecureContext,
               'mediaDevices=' + (navigator.mediaDevices ? 'ada' : 'TIDAK ADA')];
    const show = x => { d.textContent = 'browser · ' + p.concat([x]).join(' · '); };
    if (navigator.permissions && navigator.permissions.query) {
        navigator.permissions.query({name: 'camera'})
            .then(r => show('izin=' + r.state))
            .catch(() => show('izin=tidak bisa dicek'));
    } else { show('izin=tidak didukung browser ini'); }
    </script>
    """, height=40)

# ══════════════════════════════════════════════════════════════════════════════
# BELUM LOGIN
# ══════════════════════════════════════════════════════════════════════════════
else:
    # Sembunyikan Histori dari sidebar
    st.markdown("""
    <style>
    [data-testid="stSidebarNav"] ul li:nth-child(2) { display: none; }
    </style>
    """, unsafe_allow_html=True)

    if 'page' not in st.session_state:
        st.session_state['page'] = 'daftar'

    # ── MASUK ─────────────────────────────────────────────────────────────────
    if st.session_state['page'] == 'masuk':
        _, auth_col, _ = st.columns([1, 1.4, 1])
        with auth_col:
            st.markdown('<div class="auth-title">Masuk</div><div class="auth-subtitle">Sistem Pendeteksi Kantuk</div>', unsafe_allow_html=True)

            username = st.text_input("Username", placeholder="Masukkan username kamu")
            password = st.text_input("Password", type="password", placeholder="Masukkan password kamu")

            st.markdown('<div class="btn-primary-marker"></div>', unsafe_allow_html=True)
            if st.button("Masuk"):
                if not username or not password:
                    st.error("Tolong lengkapi semua field.")
                elif not check_username_exists(username):
                    st.error("Username belum terdaftar. Silakan daftar terlebih dahulu.")
                elif check_login(username, sha256(password.encode()).hexdigest()):
                    set_cookie(username)
                    st.success("Masuk berhasil!")
                    st.experimental_rerun()
                else:
                    st.error("Password salah. Silakan coba lagi.")

            st.markdown("---")
            st.markdown("Belum punya akun?")
            if st.button("Daftar di sini"):
                st.session_state['page'] = 'daftar'
                st.experimental_rerun()

            st.markdown('<div class="btn-link-marker"></div>', unsafe_allow_html=True)
            if st.button("Coba tanpa akun", key="guest_btn_masuk"):
                set_cookie(GUEST_COOKIE_VALUE)
                st.experimental_rerun()

    # ── DAFTAR ────────────────────────────────────────────────────────────────
    elif st.session_state['page'] == 'daftar':
        _, auth_col, _ = st.columns([1, 1.4, 1])
        with auth_col:
            st.markdown('<div class="auth-title">Daftar Akun</div><div class="auth-subtitle">Buat akun baru kamu</div>', unsafe_allow_html=True)

            # ── Form daftar — disembunyikan begitu kode OTP sudah terkirim ──
            if not st.session_state.get('otp_step'):
                username = st.text_input("Username", placeholder="Buat username kamu")
                password = st.text_input("Password", type="password", placeholder="Buat password kamu")

                # `key` di sini bikin Streamlit rerun otomatis begitu field ini
                # kehilangan fokus (blur), dan session_state[key] sudah berisi
                # nilai terbaru SEBELUM baris ini jalan — jadi validasinya bisa
                # langsung dibaca duluan, sebelum field-nya sendiri dirender,
                # tanpa perlu tombol submit atau rerun manual.
                email_now = st.session_state.get('reg_email_input', '').strip()
                email_invalid = bool(email_now) and not is_valid_email(email_now)

                if email_invalid:
                    st.markdown('<div class="field-error-marker"></div>', unsafe_allow_html=True)
                email = st.text_input(
                    "Email orang terdekat (opsional)",
                    placeholder="contoh: keluarga@gmail.com",
                    key="reg_email_input",
                    help="Jika diisi, email ini akan menerima notifikasi darurat saat kantuk "
                         "terdeteksi. Boleh dikosongkan jika tidak diperlukan."
                )
                if email_invalid:
                    st.markdown('<p class="field-error-text">Masukkan alamat email yang valid.</p>', unsafe_allow_html=True)

                sending_otp = st.session_state.get('sending_otp', False)

                st.markdown('<div class="btn-primary-marker"></div>', unsafe_allow_html=True)
                if sending_otp:
                    st.button("Mengirim...", disabled=True, key="send_otp_btn_sending")
                elif st.button("Daftar", key="send_otp_btn"):
                    if not username or not password:
                        st.error("Tolong lengkapi username dan password.")
                    elif check_username_exists(username):
                        st.error("Username sudah digunakan. Coba username lain.")
                    elif email_invalid:
                        pass  # sudah kelihatan di teks merah bawah field-nya
                    elif not email.strip():
                        # Tanpa email — daftar langsung, tanpa verifikasi OTP
                        pw_hash = sha256(password.encode()).hexdigest()
                        add_user(username, "", pw_hash)
                        set_cookie(username)
                        st.success("Akun berhasil dibuat! Selamat datang 🎉")
                        st.experimental_rerun()
                    else:
                        st.session_state['sending_otp']       = True
                        st.session_state['pending_username']  = username
                        st.session_state['pending_email']     = email
                        st.session_state['pending_password']  = password
                        st.experimental_rerun()

                if sending_otp:
                    otp = generate_otp()
                    pw_hash = sha256(st.session_state['pending_password'].encode()).hexdigest()
                    save_otp(st.session_state['pending_email'], otp, st.session_state['pending_username'], pw_hash)
                    success = send_otp_email(st.session_state['pending_email'], otp, st.session_state['pending_username'])
                    st.session_state['sending_otp'] = False
                    if success:
                        st.session_state['reg_username']      = st.session_state['pending_username']
                        st.session_state['reg_email']         = st.session_state['pending_email']
                        st.session_state['reg_password_hash'] = pw_hash
                        st.session_state['otp_step']          = True
                        st.session_state['otp_last_sent']     = time.time()
                    else:
                        st.session_state['send_otp_failed'] = True
                    st.experimental_rerun()

                if st.session_state.pop('send_otp_failed', False):
                    st.error("Gagal mengirim email. Periksa kembali alamat email dan coba lagi.")

            # ── Verifikasi OTP — muncul menggantikan form, tanpa pindah halaman ──
            else:
                st.markdown('<p class="section-label">Verifikasi</p>', unsafe_allow_html=True)
                st.markdown(f"Kode dikirim ke **{st.session_state['reg_email']}**")

                st.markdown('<div class="btn-link-marker"></div>', unsafe_allow_html=True)
                if st.button("Ganti email", key="change_email_btn"):
                    st.session_state['otp_step'] = False
                    st.experimental_rerun()

                otp_input = st.text_input("Masukkan Kode OTP", placeholder="6 digit kode dari email",
                                          max_chars=6, key="otp_input")

                verifying = st.session_state.get('verifying_otp', False)

                st.markdown('<div class="btn-primary-marker"></div>', unsafe_allow_html=True)
                if verifying:
                    st.button("Memverifikasi...", disabled=True, key="verify_otp_btn_busy")
                elif st.button("Verifikasi & Buat Akun", key="verify_otp_btn"):
                    # Kerja beratnya ditunda ke render berikutnya supaya tombol
                    # sempat berubah jadi "Memverifikasi..." dan terkunci —
                    # tanpa ini user tidak dapat umpan balik apa pun dan
                    # cenderung nge-spam tombolnya.
                    if not otp_input.strip():
                        st.session_state['otp_error'] = "Masukkan kode OTP terlebih dahulu."
                    else:
                        st.session_state['verifying_otp'] = True
                    st.experimental_rerun()

                if verifying:
                    with st.spinner("Memverifikasi kode..."):
                        try:
                            valid, msg = verify_otp(
                                st.session_state['reg_username'],
                                st.session_state.get('otp_input', '').strip(),
                            )
                        except Exception as e:
                            print(f"Verifikasi OTP gagal: {e}")
                            valid, msg = False, "Gagal menghubungi server. Periksa koneksi internet kamu lalu coba lagi."
                    st.session_state['verifying_otp'] = False
                    if valid:
                        set_cookie(st.session_state['reg_username'])
                    else:
                        st.session_state['otp_error'] = msg
                    st.experimental_rerun()

                if st.session_state.get('otp_error'):
                    st.error(st.session_state.pop('otp_error'))

                # Tombol kirim ulang, kanan bawah, kena cooldown 30 detik
                remaining = int(30 - (time.time() - st.session_state.get('otp_last_sent', 0)))
                # Dulu di-refresh tiap 1 detik dan itu merusak layar ini di HP:
                # tiap rerun menelan tap tombol Verifikasi dan mengganggu
                # pengetikan kode (di HP nilai field baru sampai ke server
                # setelah field kehilangan fokus, jadi syarat `not otp_input`
                # tidak menolong). Angkanya cuma kosmetik, jadi jedanya
                # diperlonggar dan dimatikan total selama verifikasi berjalan.
                if remaining > 0 and not otp_input and not verifying:
                    st_autorefresh(interval=3000, limit=12, key=f"resend_countdown_{st.session_state['otp_last_sent']}")
                if remaining > 0:
                    st.markdown(f'<p style="text-align:right; font-size:0.8rem; color:var(--muted);">Kirim ulang ({remaining}s)</p>', unsafe_allow_html=True)
                else:
                    st.markdown('<div class="btn-link-marker-right"></div>', unsafe_allow_html=True)
                    if st.button("Kirim ulang", key="resend_otp_btn"):
                        with st.spinner("Mengirim kode..."):
                            otp = generate_otp()
                            save_otp(st.session_state['reg_email'], otp, st.session_state['reg_username'], st.session_state['reg_password_hash'])
                            send_otp_email(st.session_state['reg_email'], otp, st.session_state['reg_username'])
                            st.session_state['otp_last_sent'] = time.time()
                        st.experimental_rerun()

            st.markdown("---")
            st.markdown("Sudah punya akun?")
            if st.button("Masuk di sini"):
                st.session_state['page']     = 'masuk'
                st.session_state['otp_step'] = False
                st.experimental_rerun()

            st.markdown('<div class="btn-link-marker"></div>', unsafe_allow_html=True)
            if st.button("Coba tanpa akun", key="guest_btn_daftar"):
                set_cookie(GUEST_COOKIE_VALUE)
                st.experimental_rerun()
