import sqlite3
from datetime import datetime, timedelta, timezone


DB_PATH = 'user_data.db'

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

def create_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT    UNIQUE NOT NULL,
        email    TEXT    NOT NULL,
        password TEXT    NOT NULL
    )
    ''')

    # Satu baris = satu sesi berkendara (Start → Stop)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS driving_sessions (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id          INTEGER NOT NULL,
        start_time       TEXT    NOT NULL,
        end_time         TEXT,
        duration         INTEGER,          -- total detik berkendara
        total_eye_close  INTEGER DEFAULT 0,
        total_yawn       INTEGER DEFAULT 0,
        total_head_tilt  INTEGER DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    )
    ''')

    # Satu baris = satu kejadian kantuk di dalam sesi
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS drowsiness_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER NOT NULL,
        event_type  TEXT    NOT NULL,   -- 'eye_close' | 'yawn' | 'head_tilt'
        event_time  TEXT    NOT NULL,   -- waktu mulai kejadian
        duration    REAL    NOT NULL,   -- lama kejadian dalam detik
        FOREIGN KEY (session_id) REFERENCES driving_sessions (id) ON DELETE CASCADE
    )
    ''')

    conn.commit()
    conn.close()



# ─── OTP ─────────────────────────────────────────────────────────────────────

def save_otp(email, otp_code, username, password):
    """Simpan OTP sementara sebelum akun dikonfirmasi."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS pending_registrations (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        username   TEXT NOT NULL,
        email      TEXT NOT NULL,
        password   TEXT NOT NULL,
        otp_code   TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    ''')
    # Hapus pending lama untuk username/email yang sama
    cursor.execute('DELETE FROM pending_registrations WHERE username=? OR email=?', (username, email))
    cursor.execute(
        'INSERT INTO pending_registrations (username, email, password, otp_code, created_at) VALUES (?, ?, ?, ?, ?)',
        (username, email, password, otp_code, now_wib().strftime('%Y-%m-%d %H:%M:%S'))
    )
    conn.commit()
    conn.close()

def verify_otp(username, otp_input):
    """
    Verifikasi OTP. Jika valid dan belum expired (10 menit),
    buat akun dan hapus data pending.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        'SELECT email, password, otp_code, created_at FROM pending_registrations WHERE username=?',
        (username,)
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, "Data pendaftaran tidak ditemukan."

    email, password, otp_code, created_at = row

    # Cek expired (10 menit)
    created_dt = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
    if (now_wib() - created_dt).total_seconds() > 600:
        cursor.execute('DELETE FROM pending_registrations WHERE username=?', (username,))
        conn.commit()
        conn.close()
        return False, "Kode OTP sudah kadaluarsa. Silakan daftar ulang."

    if otp_input.strip() != otp_code:
        conn.close()
        return False, "Kode OTP salah. Silakan coba lagi."

    # OTP valid — buat akun
    cursor.execute(
        'INSERT INTO users (username, email, password) VALUES (?, ?, ?)',
        (username, email, password)
    )
    cursor.execute('DELETE FROM pending_registrations WHERE username=?', (username,))
    conn.commit()
    conn.close()
    return True, "Akun berhasil dibuat!"

# ─── Users ────────────────────────────────────────────────────────────────────

def add_user(username, email, password):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO users (username, email, password) VALUES (?, ?, ?)',
        (username, email, password)
    )
    conn.commit()
    conn.close()

def check_username_exists(username):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM users WHERE username=?', (username,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def check_login(username, password):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM users WHERE username=? AND password=?', (username, password))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def get_user_id_by_username(username):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM users WHERE username=?', (username,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def get_user_email(username):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT email FROM users WHERE username=?', (username,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None


# ─── Sessions ─────────────────────────────────────────────────────────────────

def start_session(username):
    """
    Dipanggil saat user klik START.
    Membuat sesi baru dan mengembalikan session_id.
    """
    user_id = get_user_id_by_username(username)
    if user_id is None:
        return None

    start_time = now_wib().strftime('%Y-%m-%d %H:%M:%S')
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO driving_sessions (user_id, start_time) VALUES (?, ?)',
        (user_id, start_time)
    )
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return session_id

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

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Hitung durasi sesi
    cursor.execute('SELECT start_time FROM driving_sessions WHERE id=?', (session_id,))
    row = cursor.fetchone()
    duration = 0
    if row:
        start_dt = datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
        end_dt   = datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S')
        duration = int((end_dt - start_dt).total_seconds())

    total_eye_close, total_yawn, total_head_tilt = compute_session_totals(events)

    # Update sesi
    cursor.execute('''
        UPDATE driving_sessions
        SET end_time=?, duration=?,
            total_eye_close=?, total_yawn=?, total_head_tilt=?
        WHERE id=?
    ''', (end_time, duration, total_eye_close, total_yawn, total_head_tilt, session_id))

    # Push semua event
    for e in events:
        cursor.execute('''
            INSERT INTO drowsiness_events (session_id, event_type, event_time, duration)
            VALUES (?, ?, ?, ?)
        ''', (session_id, e['event_type'], e['event_time'], e['duration']))

    conn.commit()
    conn.close()


# ─── Getters untuk halaman Histori ────────────────────────────────────────────

def get_sessions_by_username(username):
    """Ambil semua sesi berkendara milik user, terbaru di atas."""
    user_id = get_user_id_by_username(username)
    if user_id is None:
        return []
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, start_time, end_time, duration,
               total_eye_close, total_yawn, total_head_tilt
        FROM driving_sessions
        WHERE user_id=?
        ORDER BY start_time DESC
    ''', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_events_by_session(session_id):
    """Ambil semua event kantuk dalam satu sesi."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT event_type, event_time, duration
        FROM drowsiness_events
        WHERE session_id=?
        ORDER BY event_time ASC
    ''', (session_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows