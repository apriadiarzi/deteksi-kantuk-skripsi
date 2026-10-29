import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2
from decouple import config


# URL database Neon PostgreSQL, dari Secrets (Streamlit Cloud) atau .env (lokal).
DATABASE_URL = config('DATABASE_URL')

# Satu koneksi dipakai ulang untuk menghindari biaya handshake di setiap query.
_conn = None
_conn_lock = threading.Lock()

@contextmanager
def _db(commit=False):
    """Pinjam cursor dari koneksi bersama; koneksi yang terputus dibuat ulang sekali."""
    global _conn
    with _conn_lock:
        cursor = None
        for attempt in (1, 2):
            try:
                baru = _conn is None or _conn.closed
                if baru:
                    _conn = psycopg2.connect(DATABASE_URL)
                cursor = _conn.cursor()
                if not baru:
                    # Ping untuk memastikan koneksi lama masih hidup.
                    cursor.execute('SELECT 1')
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                try:
                    if _conn is not None:
                        _conn.close()
                except Exception:
                    pass
                _conn = None
                cursor = None
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
    """Waktu sekarang dalam WIB (UTC+7), tidak tergantung timezone server."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=7)

# ─── Mode Tamu ────────────────────────────────────────────────────────────────
# Mode tamu ditandai lewat penyimpanan login yang sama dengan akun biasa.
GUEST_COOKIE_VALUE = "__guest__"
GUEST_STORAGE_KEY = "drowsiness_guest_sessions"

# Key localStorage untuk token login di browser.
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

# Skema cukup dibuat sekali per proses.
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
    """Verifikasi OTP (berlaku 10 menit); jika valid, buat akun dan hapus data pending."""
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


# ─── Token login ──────────────────────────────────────────────────────────────

def set_session_token(username):
    """Buat token login acak untuk akun ini dan simpan di database."""
    token = secrets.token_hex(32)
    with _db(commit=True) as cursor:
        cursor.execute('UPDATE users SET session_token=%s WHERE username=%s', (token, username))
    return token

def get_username_by_token(token):
    """Cari pemilik token login."""
    if not token:
        return None
    with _db() as cursor:
        cursor.execute('SELECT username FROM users WHERE session_token=%s', (token,))
        result = cursor.fetchone()
        return result[0] if result else None

def clear_session_token(username):
    """Hapus token login saat logout."""
    if not username:
        return
    with _db(commit=True) as cursor:
        cursor.execute('UPDATE users SET session_token=NULL WHERE username=%s', (username,))


# ─── Sessions ─────────────────────────────────────────────────────────────────

def start_session(username):
    """Buat sesi berkendara baru dan kembalikan id-nya."""
    start_time = now_wib().strftime('%Y-%m-%d %H:%M:%S')
    # Id user dicari langsung di dalam INSERT (satu query).
    with _db(commit=True) as cursor:
        cursor.execute('''
            INSERT INTO driving_sessions (user_id, start_time)
            SELECT id, %s FROM users WHERE username=%s
            RETURNING id
        ''', (start_time, username))
        row = cursor.fetchone()
        return row[0] if row else None

# Total per jenis kejadian dihitung dari tabel drowsiness_events.
_TOTALS_FROM_EVENTS = '''
    total_eye_close = (SELECT count(*) FROM drowsiness_events e
                       WHERE e.session_id = s.id AND e.event_type = 'eye_close'),
    total_yawn      = (SELECT count(*) FROM drowsiness_events e
                       WHERE e.session_id = s.id AND e.event_type = 'yawn'),
    total_head_tilt = (SELECT count(*) FROM drowsiness_events e
                       WHERE e.session_id = s.id AND e.event_type = 'head_tilt')
'''

def save_session_progress(session_id, new_events: list):
    """Simpan kejadian baru sesi ini lalu perbarui waktu selesai, durasi, dan totalnya."""
    if session_id is None:
        return

    end_time = now_wib().strftime('%Y-%m-%d %H:%M:%S')

    # Satu transaksi untuk insert kejadian dan update sesi.
    with _db(commit=True) as cursor:
        if new_events:
            cursor.executemany('''
                INSERT INTO drowsiness_events (session_id, event_type, event_time, duration)
                VALUES (%s, %s, %s, %s)
            ''', [(session_id, e['event_type'], e['event_time'], e['duration']) for e in new_events])

        cursor.execute(f'''
            UPDATE driving_sessions s
            SET end_time = %s,
                duration = EXTRACT(EPOCH FROM (%s::timestamp - s.start_time::timestamp))::int,
                {_TOTALS_FROM_EVENTS}
            WHERE s.id = %s
        ''', (end_time, end_time, session_id))

def end_session(session_id, remaining_events: list):
    """Simpan sisa kejadian dan perbarui ringkasan sesi saat sesi ditutup."""
    save_session_progress(session_id, remaining_events)


# ─── Getters untuk halaman Histori ────────────────────────────────────────────

def get_sessions_by_username(username):
    """Ambil semua sesi berkendara milik user, terbaru di atas."""
    # Total kejadian dihitung langsung dari tabel drowsiness_events.
    with _db() as cursor:
        cursor.execute('''
            SELECT s.id, s.start_time, s.end_time, s.duration,
                   (SELECT count(*) FROM drowsiness_events e
                    WHERE e.session_id = s.id AND e.event_type = 'eye_close'),
                   (SELECT count(*) FROM drowsiness_events e
                    WHERE e.session_id = s.id AND e.event_type = 'yawn'),
                   (SELECT count(*) FROM drowsiness_events e
                    WHERE e.session_id = s.id AND e.event_type = 'head_tilt')
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
