import os
import av
import threading
import streamlit as st
import streamlit_nested_layout
from streamlit_webrtc import VideoHTMLAttributes, webrtc_streamer
import streamlit.components.v1 as components

from audio_handling import AudioFrameHandler
from deteksi_kantuk import VideoFrameHandler, set_location_cache

import sqlite3
from database import create_db, add_user, check_username_exists, check_login
from hashlib import sha256
import pickle
import time

create_db()

def set_cookie(username):
    cookies = {"username": username, "expiry": time.time() + 60 * 60 * 24 * 30}
    pickle.dump(cookies, open("cookies.pkl", "wb"))

def get_cookie():
    if os.path.exists("cookies.pkl"):
        cookies = pickle.load(open("cookies.pkl", "rb"))
        if cookies["expiry"] > time.time():
            return cookies["username"]
    return None

user = get_cookie()

query_params = st.experimental_get_query_params()
if "loc" in query_params:
    set_location_cache(query_params["loc"][0])

if user:
    alarm_file_path = os.path.join("audio", "wake_up.wav")

    st.set_page_config(
        page_title="Sistem pendeteksi kantuk",
        page_icon="https://cdn-icons-png.flaticon.com/512/1464/1464723.png",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # ── GPS realtime dari browser ─────────────────────────────────────────────
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

    if st.sidebar.button("Logout"):
        set_cookie("")

    col1, col2 = st.columns(spec=[6, 2], gap="medium")

    with col1:
        st.title("Sistem Pendeteksi Kantuk Untuk Pengendara Roda Empat")

        # ── Baris 1: Waktu alarm & EAR ────────────────────────────────────────
        with st.container():
            c1, c2 = st.columns(2)
            with c1:
                WAIT_TIME = st.slider(
                    "Waktu yang dibutuhkan alarm untuk menyala (detik):",
                    0.0, 5.0, 1.0, 0.25,
                    help=(
                        "Berapa lama kondisi kantuk harus berlangsung sebelum alarm berbunyi. "
                        "Berlaku untuk deteksi mata tertutup, menguap, dan kemiringan kepala. "
                        "Referensi: Wierwille & Ellsworth (1994) menyarankan 1–2 detik untuk "
                        "menghindari false alarm."
                    )
                )
            with c2:
                EAR_THRESH = st.slider(
                    "Ambang Batas Eye Aspect Ratio (EAR):",
                    0.0, 0.4, 0.18, 0.01,
                    help=(
                        "Nilai EAR di bawah threshold = mata tertutup = mengantuk. "
                        "Referensi: Soukupová & Čech (2016) menyarankan 0.2–0.25. "
                        "Turunkan nilainya jika terlalu sensitif, naikkan jika kurang sensitif."
                    )
                )

        # ── Baris 2: MAR & Head Tilt ──────────────────────────────────────────
        with st.container():
            c3, c4 = st.columns(2)
            with c3:
                MAR_THRESH = st.slider(
                    "Ambang Batas Mouth Aspect Ratio (MAR):",
                    0.3, 1.0, 0.6, 0.01,
                    help=(
                        "Nilai MAR di atas threshold = mulut terbuka lebar = menguap. "
                        "Referensi: Mandal et al. (2017) & Vural et al. (2007) "
                        "menyarankan threshold 0.5–0.7, default 0.6."
                    )
                )
            with c4:
                TILT_THRESH = st.slider(
                    "Ambang Batas Kemiringan Kepala (derajat):",
                    5.0, 45.0, 20.0, 1.0,
                    help=(
                        "Sudut kemiringan kepala dari posisi tegak. "
                        "Referensi: Sahayadhas et al. (2012) — kemiringan > 15° = kantuk ringan, "
                        "> 25°–30° = kantuk berat. "
                        "Default 20° sebagai nilai tengah yang direkomendasikan. "
                        "Turunkan nilai untuk deteksi lebih sensitif."
                    )
                )

    thresholds = {
        "EAR_THRESH":  EAR_THRESH,
        "WAIT_TIME":   WAIT_TIME,
        "MAR_THRESH":  MAR_THRESH,
        "TILT_THRESH": TILT_THRESH,
    }

    video_handler = VideoFrameHandler()
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
        new_frame = audio_handler.process(frame, play_sound=play_alarm)
        return new_frame

    with col1:
        ctx = webrtc_streamer(
            key="drowsiness-detection",
            video_frame_callback=video_frame_callback,
            audio_frame_callback=audio_frame_callback,
            rtc_configuration={"iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]},
            media_stream_constraints={"video": {"height": {"ideal": 480}}, "audio": True},
            video_html_attrs=VideoHTMLAttributes(autoPlay=True, controls=False, muted=False),
        )

else:
    def set_page(page):
        st.session_state['page'] = page

    if 'page' not in st.session_state:
        st.session_state['page'] = 'daftar'

    if st.session_state['page'] == 'masuk':
        st.subheader("Masuk")
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")

        if st.button("Masuk"):
            if not username or not password:
                st.error("Tolong lengkapi semua field")
            else:
                if check_login(username, sha256(password.encode()).hexdigest()):
                    set_cookie(username)
                    st.success("Masuk berhasil!")
                    st.rerun()
                else:
                    st.error("Username/password salah")

        col1, col2 = st.columns([0.25, 1])
        with col1:
            st.markdown("Belum punya akun?")
        with col2:
            if st.button("Daftar disini"):
                st.session_state['page'] = 'daftar'
                st.rerun()

    if st.session_state['page'] == 'daftar':
        st.subheader("Daftar")
        username = st.text_input("Username")
        email    = st.text_input("Email orang terdekat")
        password = st.text_input("Password", type="password")

        if st.button("Daftar"):
            if not username or not email or not password:
                st.error("Tolong lengkapi semua field")
            elif check_username_exists(username):
                st.error("Username sudah digunakan, mohon gunakan username lain")
            else:
                add_user(username, email, sha256(password.encode()).hexdigest())
                set_cookie(username)
                st.rerun()

        col1, col2 = st.columns([0.25, 1])
        with col1:
            st.markdown("Sudah punya akun?")
        with col2:
            if st.button("Masuk disini"):
                st.session_state['page'] = 'masuk'
                st.rerun()