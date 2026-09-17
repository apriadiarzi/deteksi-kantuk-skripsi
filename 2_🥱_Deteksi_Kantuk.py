import os
import re
import av
import random
import string
import threading
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
)
from hashlib import sha256
from decouple import config
import pickle
import time
import json
from datetime import datetime

create_db()

# ── Konstanta email pengirim ──────────────────────────────────────────────────
EMAIL_SENDER   = "deteksikantuk@gmail.com"
EMAIL_PASSWORD = config('EMAIL_PASSWORD')

# Berapa detik kamera harus benar-benar mati sebelum sesi dianggap selesai.
CAMERA_OFF_GRACE = 3.0

# ── Cookie helper ─────────────────────────────────────────────────────────────
def set_cookie(username):
    cookies = {"username": username, "expiry": time.time() + 60 * 60 * 24 * 30}
    pickle.dump(cookies, open("cookies.pkl", "wb"))

def get_cookie():
    if os.path.exists("cookies.pkl"):
        cookies = pickle.load(open("cookies.pkl", "rb"))
        if cookies["expiry"] > time.time():
            return cookies["username"]
    return None

def save_guest_session(session_id, start_time_str, events):
    """Simpan satu sesi tamu ke localStorage browser (maksimal 10 sesi terbaru)."""
    end_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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
user, is_guest = resolve_guest_user(get_cookie())
logged_in = bool(user or is_guest)

query_params = st.experimental_get_query_params()
if "loc" in query_params:
    set_location_cache(query_params["loc"][0])

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Sistem Pendeteksi Kantuk",
    page_icon="https://cdn-icons-png.flaticon.com/512/1464/1464723.png",
    layout="wide",
    initial_sidebar_state="expanded" if logged_in else "collapsed",
)
st.markdown(MOBILE_CSS, unsafe_allow_html=True)

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
        frame = frame.to_ndarray(format="bgr24")
        frame, play_alarm = video_handler.process(frame, thresholds)
        with lock:
            shared_state["play_alarm"] = play_alarm
        return av.VideoFrame.from_ndarray(frame, format="bgr24")

    def audio_frame_callback(frame: av.AudioFrame):
        with lock:
            play_alarm = shared_state["play_alarm"]
        return audio_handler.process(frame, play_sound=play_alarm)

    ctx = webrtc_streamer(
        key="drowsiness-detection",
        video_frame_callback=video_frame_callback,
        audio_frame_callback=audio_frame_callback,
        rtc_configuration={"iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]},
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
            st.session_state["guest_session_start"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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

                st.markdown('<div class="btn-primary-marker"></div>', unsafe_allow_html=True)
                if st.button("Verifikasi & Buat Akun", key="verify_otp_btn"):
                    if not otp_input:
                        st.error("Masukkan kode OTP terlebih dahulu.")
                    else:
                        valid, msg = verify_otp(st.session_state['reg_username'], otp_input)
                        if valid:
                            set_cookie(st.session_state['reg_username'])
                            st.success("Akun berhasil dibuat! Selamat datang 🎉")
                            st.experimental_rerun()
                        else:
                            st.error(msg)

                # Tombol kirim ulang, kanan bawah, kena cooldown 30 detik
                remaining = int(30 - (time.time() - st.session_state.get('otp_last_sent', 0)))
                # Auto-refresh tiap detik biar angkanya jalan — jeda kalau lagi ngetik OTP
                # key & limit tetap per siklus kirim, biar hitungan internal komponennya ga ke-lap
                if remaining > 0 and not otp_input:
                    st_autorefresh(interval=1000, limit=35, key=f"resend_countdown_{st.session_state['otp_last_sent']}")
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
