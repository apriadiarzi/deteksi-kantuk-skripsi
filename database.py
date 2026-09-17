import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2
from decouple import config


# Koneksi Postgres (Neon). Diisi lewat Secrets di Streamlit Cloud dan lewat
# .env di lokal — keduanya boleh (malah sebaiknya) menunjuk ke database yang
# sama, supaya akun yang didaftarkan di lokal juga langsung ada di web.
#
# Kenapa bukan SQLite lagi: filesystem Streamlit Community Cloud itu ephemeral.
# Container-nya dibangun ulang dari repo GitHub tiap redeploy/reboot, jadi file
# user_data.db selalu ketimpa balik ke versi yang ada di git — akun yang
# didaftarkan lewat web selalu hilang. Database eksternal hidup di luar
# container, jadi datanya tidak ikut ter-reset.
DATABASE_URL = config('DATABASE_URL')

# Koneksinya dipakai ulang, bukan dibuka-tutup tiap pemanggilan fungsi.
# Alasannya terukur: membuka koneksi baru ke Neon makan ~380 ms (handshake
# TCP+TLS) sementara query-nya sendiri cuma ~19 ms — 95% biayanya di handshake.
# Dari Streamlit Cloud (server di Amerika, database di Singapura) angka itu
# berlipat, dan tiap panggilan DB ikut menahan jalannya script Streamlit.
_conn = None
_conn_lock = threading.Lock()

@contextmanager
def _db(commit=False):
    """Pinjam cursor dari koneksi bersama.

    Neon menidurkan database saat menganggur, jadi koneksi yang tersimpan bisa
    basi tanpa pemberitahuan. Kalau itu terjadi, koneksinya dibuang dan operasi
    diulang sekali dengan koneksi baru.
    """
    global _conn
    with _conn_lock:
        for attempt in (1, 2):
            try:
                if _conn is None or _conn.closed:
                    _conn = psycopg2.connect(DATABASE_URL)
                cursor = _conn.cursor()
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                _conn = None
                if attempt == 2:
                    raise
        try:
            yield cursor
            if commit:
                _conn.commit()
        except Exception:
            if commit:
                try:
                    _conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            try:
                cursor.close()
            except Exception:
                pass

def now_wib():
    """Waktu sekarang di WIB (UTC+7), dihitung dari UTC — jadi tidak
    tergantung timezone server (server cloud biasanya UTC, laptop lokal bisa
    beda-beda), supaya jam yang tersimpan selalu konsisten di WIB."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=7)

# ─── Mode Tamu ────────────────────────────────────────────────────────────────
# Ditandai lewat cookie yang sama dengan login biasa (bukan session_state) agar
# tetap "nyantol" walau tab di-refresh atau dibuka ulang.
GUEST_COOKIE_VALUE = "__guest__"
GUEST_STORAGE_KEY = "drowsiness_guest_sessions"

# Key localStorage tempat token "ingat saya" disimpan di browser masing-masing
# pengunjung (lihat set_session_token/get_username_by_token). Ini BUKAN
# cookies.pkl versi baru — localStorage bawaannya sudah per-browser/per-origin,
# tidak pernah dibagi antar pengunjung, dan isinya cuma token acak yang harus
# cocok dengan DB, bukan username mentah yang bisa dipalsukan lewat DevTools.
AUTH_STORAGE_KEY = "drowsiness_auth_token"

def resolve_guest_user(cookie_value):
    """Dari nilai cookie mentah, kembalikan (user, is_guest)."""
    if cookie_value == GUEST_COOKIE_VALUE:
        return None, True
    return cookie_value, False

def compute_session_totals(events):
    """Hitung total kejadian per jenis dalam satu pass."""
    totals = {"eye_close": 0, "yawn": 0, "head_tilt": 0}
    for e in events:
        if e['event_type'] in totals:
            totals[e['event_type']] += 1
    return totals["eye_close"], totals["yawn"], totals["head_tilt"]

# ─── Setup ────────────────────────────────────────────────────────────────────

# create_db() dipanggil di baris atas halaman utama, artinya kena tiap kali
# script dijalankan ulang — termasuk tiap detik saat autorefresh monitoring
# aktif. Cukup sekali saja per proses.
_schema_ready = False

def create_db():
    global _schema_ready
    if _schema_ready:
        return

    with _db(commit=True) as cursor:
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id            SERIAL PRIMARY KEY,
            username      TEXT UNIQUE NOT NULL,
            email         TEXT NOT NULL,
            password      TEXT NOT NULL,
            session_token TEXT
        )
        ''')

        # Satu baris = satu sesi berkendara (Start → Stop)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS driving_sessions (
            id               SERIAL PRIMARY KEY,
            user_id          INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            start_time       TEXT    NOT NULL,
            end_time         TEXT,
            duration         INTEGER,          -- total detik berkendara
            total_eye_close  INTEGER DEFAULT 0,
            total_yawn       INTEGER DEFAULT 0,
            total_head_tilt  INTEGER DEFAULT 0
        )
        ''')

        # Satu baris = satu kejadian kantuk di dalam sesi
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS drowsiness_events (
            id          SERIAL PRIMARY KEY,
            session_id  INTEGER NOT NULL REFERENCES driving_sessions (id) ON DELETE CASCADE,
            event_type  TEXT    NOT NULL,   -- 'eye_close' | 'yawn' | 'head_tilt'
            event_time  TEXT    NOT NULL,   -- waktu mulai kejadian
            duration    REAL    NOT NULL    -- lama kejadian dalam detik
        )
        ''')

        # Pendaftaran yang belum diverifikasi OTP
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_registrations (
            id         SERIAL PRIMARY KEY,
            username   TEXT NOT NULL,
            email      TEXT NOT NULL,
            password   TEXT NOT NULL,
            otp_code   TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        ''')

    _schema_ready = True


# ─── OTP ─────────────────────────────────────────────────────────────────────

def save_otp(email, otp_code, username, password):
    """Simpan OTP sementara sebelum akun dikonfirmasi."""
    with _db(commit=True) as cursor:
        # Hapus pending lama untuk username/email yang sama
        cursor.execute('DELETE FROM pending_registrations WHERE username=%s OR email=%s', (username, email))
        cursor.execute(
            'INSERT INTO pending_registrations (username, email, password, otp_code, created_at) VALUES (%s, %s, %s, %s, %s)',
            (username, email, password, otp_code, now_wib().strftime('%Y-%m-%d %H:%M:%S'))
        )

def verify_otp(username, otp_input):
    """
    Verifikasi OTP. Jika valid dan belum expired (10 menit),
    buat akun dan hapus data pending.
    """
    with _db(commit=True) as cursor:
        cursor.execute(
            'SELECT email, password, otp_code, created_at FROM pending_registrations WHERE username=%s',
            (username,)
        )
        row = cursor.fetchone()
        if not row:
            return False, "Data pendaftaran tidak ditemukan."

        email, password, otp_code, created_at = row

        # Cek expired (10 menit)
        created_dt = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
        if (now_wib() - created_dt).total_seconds() > 600:
            cursor.execute('DELETE FROM pending_registrations WHERE username=%s', (username,))
            return False, "Kode OTP sudah kadaluarsa. Silakan daftar ulang."

        if otp_input.strip() != otp_code:
            return False, "Kode OTP salah. Silakan coba lagi."

        # OTP valid — buat akun
        cursor.execute(
            'INSERT INTO users (username, email, password) VALUES (%s, %s, %s)',
            (username, email, password)
        )
        cursor.execute('DELETE FROM pending_registrations WHERE username=%s', (username,))
        return True, "Akun berhasil dibuat!"

# ─── Users ────────────────────────────────────────────────────────────────────

def add_user(username, email, password):
    with _db(commit=True) as cursor:
        cursor.execute(
            'INSERT INTO users (username, email, password) VALUES (%s, %s, %s)',
            (username, email, password)
        )

def check_username_exists(username):
    with _db() as cursor:
        cursor.execute('SELECT id FROM users WHERE username=%s', (username,))
        return cursor.fetchone() is not None

def check_login(username, password):
    with _db() as cursor:
        cursor.execute('SELECT id FROM users WHERE username=%s AND password=%s', (username, password))
        return cursor.fetchone() is not None

def get_user_id_by_username(username):
    with _db() as cursor:
        cursor.execute('SELECT id FROM users WHERE username=%s', (username,))
        result = cursor.fetchone()
        return result[0] if result else None

def get_user_email(username):
    with _db() as cursor:
        cursor.execute('SELECT email FROM users WHERE username=%s', (username,))
        result = cursor.fetchone()
        return result[0] if result else None


# ─── Token "ingat saya" (persist login lewat localStorage, BUKAN cookies.pkl) ──
# Satu token per akun (bukan per-device) — simpel & cukup untuk skala thesis
# ini. Konsekuensinya: login di browser/device baru akan membuat token lama
# di device lain berhenti berlaku (bukan bug keamanan, cuma harus login ulang).

def set_session_token(username):
    """Buat token acak baru, simpan di DB untuk akun ini, kembalikan tokennya
    supaya bisa didorong ke localStorage browser yang bersangkutan."""
    token = secrets.token_hex(32)
    with _db(commit=True) as cursor:
        cursor.execute('UPDATE users SET session_token=%s WHERE username=%s', (token, username))
    return token

def get_username_by_token(token):
    """Cari pemilik token — dipakai untuk memulihkan login dari localStorage.
    Token acak & tersimpan di DB, jadi tidak bisa dipalsukan cuma dengan
    menebak/mengetik username di localStorage lewat DevTools."""
    if not token:
        return None
    with _db() as cursor:
        cursor.execute('SELECT username FROM users WHERE session_token=%s', (token,))
        result = cursor.fetchone()
        return result[0] if result else None

def clear_session_token(username):
    """Cabut token saat logout, supaya localStorage lama (kalau tidak sempat
    terhapus di browser) tidak bisa dipakai untuk login lagi."""
    if not username:
        return
    with _db(commit=True) as cursor:
        cursor.execute('UPDATE users SET session_token=NULL WHERE username=%s', (username,))


# ─── Sessions ─────────────────────────────────────────────────────────────────

def start_session(username):
    """
    Dipanggil saat user klik START.
    Membuat sesi baru dan mengembalikan session_id.
    """
    start_time = now_wib().strftime('%Y-%m-%d %H:%M:%S')
    # Satu perjalanan ke database saja, bukan dua: id user dicari langsung di
    # dalam INSERT-nya. Fungsi ini jalan tepat saat kamera mulai menyala, jadi
    # jangan sampai menahan script lebih lama dari perlunya.
    with _db(commit=True) as cursor:
        cursor.execute('''
            INSERT INTO driving_sessions (user_id, start_time)
            SELECT id, %s FROM users WHERE username=%s
            RETURNING id
        ''', (start_time, username))
        row = cursor.fetchone()
        return row[0] if row else None

def end_session(session_id, events: list):
    """
    Dipanggil saat user klik STOP.
    Menutup sesi dan push semua event sekaligus ke database.

    events = list of dict:
        {
            'event_type': 'eye_close' | 'yawn' | 'head_tilt',
            'event_time': '2025-01-01 08:10:20',
            'duration': 3.2
        }
    """
    if session_id is None:
        return

    end_time = now_wib().strftime('%Y-%m-%d %H:%M:%S')
    total_eye_close, total_yawn, total_head_tilt = compute_session_totals(events)

    with _db(commit=True) as cursor:
        # Hitung durasi sesi
        cursor.execute('SELECT start_time FROM driving_sessions WHERE id=%s', (session_id,))
        row = cursor.fetchone()
        duration = 0
        if row:
            start_dt = datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
            end_dt   = datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S')
            duration = int((end_dt - start_dt).total_seconds())

        cursor.execute('''
            UPDATE driving_sessions
            SET end_time=%s, duration=%s,
                total_eye_close=%s, total_yawn=%s, total_head_tilt=%s
            WHERE id=%s
        ''', (end_time, duration, total_eye_close, total_yawn, total_head_tilt, session_id))

        # Push semua event sekaligus, bukan satu INSERT per kejadian
        if events:
            cursor.executemany('''
                INSERT INTO drowsiness_events (session_id, event_type, event_time, duration)
                VALUES (%s, %s, %s, %s)
            ''', [(session_id, e['event_type'], e['event_time'], e['duration']) for e in events])


# ─── Getters untuk halaman Histori ────────────────────────────────────────────

def get_sessions_by_username(username):
    """Ambil semua sesi berkendara milik user, terbaru di atas."""
    with _db() as cursor:
        cursor.execute('''
            SELECT s.id, s.start_time, s.end_time, s.duration,
                   s.total_eye_close, s.total_yawn, s.total_head_tilt
            FROM driving_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE u.username = %s
            ORDER BY s.start_time DESC
        ''', (username,))
        return cursor.fetchall()

def get_events_by_session(session_id):
    """Ambil semua event kantuk dalam satu sesi."""
    with _db() as cursor:
        cursor.execute('''
            SELECT event_type, event_time, duration
            FROM drowsiness_events
            WHERE session_id=%s
            ORDER BY event_time ASC
        ''', (session_id,))
        return cursor.fetchall()
