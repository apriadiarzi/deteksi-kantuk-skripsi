"""Ukur waktu handshake dan query ke database Neon PostgreSQL.

Jalankan dari folder project:
    python ukur_db.py              # 10 percobaan
    python ukur_db.py --n 20       # jumlah percobaan lain
    python ukur_db.py --user NAMA  # akun yang dipakai untuk query Histori

PENTING: dijalankan dari laptop, angka ini mengukur jalur LAPTOP -> Neon. Untuk
jalur Streamlit Cloud -> Neon (yang dialami aplikasi), jalankan fungsi yang sama
dari panel "Diagnostik" di aplikasi yang sudah di-deploy dengan DIAGNOSTIK=1.

Tiap percobaan memakai koneksi BARU:
  1. handshake : psycopg2.connect() — DNS, TCP, TLS, autentikasi (dan bangun
                 dari tidur kalau compute Neon sedang suspend)
  2. SELECT    : query halaman Histori (daftar sesi + jumlah kejadian per jenis)
  3. tulis     : INSERT kejadian + UPDATE ringkasan sesi, persis seperti
                 save_session_progress(), di dalam transaksi yang DI-ROLLBACK
                 sehingga tidak ada data yang tersimpan.

Kredensial tidak pernah dicetak. Catatan: walau di-rollback, nomor urut SERIAL
(id) tetap maju — itu sifat sequence Postgres, bukan data yang tersimpan.
"""
import argparse
import statistics
import time

import psycopg2

# Sumber kredensial & SQL yang SAMA dengan aplikasi (config() dari decouple:
# .env di lokal, Secrets -> environment variable di Streamlit Cloud).
from database import DATABASE_URL, _TOTALS_FROM_EVENTS

SELECT_HISTORI = '''
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
'''  # sama dengan get_sessions_by_username() di database.py

CONTOH_KEJADIAN = [  # 3 kejadian, satu per jenis — ukuran simpanan bertahap yang wajar
    ("eye_close", "2026-01-01 08:00:01", 1.2),
    ("yawn",      "2026-01-01 08:00:05", 2.4),
    ("head_tilt", "2026-01-01 08:00:09", 1.6),
]


def _aman(e: Exception) -> str:
    """Pesan error tanpa kredensial (juga tanpa nama user & host database —
    pesan error psycopg2 menyebut host-nya, dan pesan ini bisa tampil di
    panel Diagnostik aplikasi)."""
    msg = f"{type(e).__name__}: {e}".strip()
    if DATABASE_URL:
        msg = msg.replace(DATABASE_URL, "<DATABASE_URL>")
        try:
            userinfo, rest = DATABASE_URL.split("://", 1)[1].split("@", 1)
            user, pw = (userinfo.split(":", 1) + [""])[:2]
            host = rest.split("/", 1)[0].split(":", 1)[0]
            for secret, label in ((pw, "<password>"), (host, "<host>"), (user, "<user>")):
                if secret:
                    msg = msg.replace(secret, label)
        except (IndexError, ValueError):
            pass
    return msg


def _pilih_akun(conn):
    """Akun dengan sesi terbanyak, supaya query Histori tidak kosong."""
    with conn.cursor() as cur:
        cur.execute('''
            SELECT u.username, u.id, count(s.id) AS n
            FROM users u LEFT JOIN driving_sessions s ON s.user_id = u.id
            GROUP BY u.id ORDER BY n DESC LIMIT 1
        ''')
        row = cur.fetchone()
    conn.rollback()
    return row


def ukur(n: int = 10, username: str = None):
    """Jalankan n percobaan. Mengembalikan dict berisi baris per percobaan."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL tidak ditemukan (cek .env / Secrets).")

    # Persiapan (tidak ikut diukur): akun & user_id untuk baris sesi sementara.
    setup = psycopg2.connect(DATABASE_URL)
    try:
        if username:
            with setup.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE username=%s", (username,))
                r = cur.fetchone()
                if not r:
                    raise RuntimeError("Akun untuk query Histori tidak ditemukan.")
                user_id = r[0]
                cur.execute("SELECT count(*) FROM driving_sessions WHERE user_id=%s", (user_id,))
                n_sesi = cur.fetchone()[0]
            setup.rollback()
        else:
            username, user_id, n_sesi = _pilih_akun(setup)
    finally:
        setup.close()

    rows = []
    for i in range(1, n + 1):
        t0 = time.perf_counter()
        conn = psycopg2.connect(DATABASE_URL)
        t_handshake = (time.perf_counter() - t0) * 1000
        try:
            # SELECT halaman Histori. Autocommit supaya yang terukur cuma query
            # itu sendiri: tanpa autocommit, psycopg2 mengirim BEGIN terpisah
            # sebelum query pertama — satu perjalanan bolak-balik tambahan.
            conn.autocommit = True
            with conn.cursor() as cur:
                t0 = time.perf_counter()
                cur.execute(SELECT_HISTORI, (username,))
                cur.fetchall()
                t_select = (time.perf_counter() - t0) * 1000
            conn.autocommit = False

            with conn.cursor() as cur:
                # Baris sesi sementara (tidak diukur, sekaligus membuka
                # transaksinya) — supaya UPDATE di bawah
                # tidak menyentuh sesi milik siapa pun.
                cur.execute('''
                    INSERT INTO driving_sessions (user_id, start_time)
                    VALUES (%s, '2026-01-01 08:00:00') RETURNING id
                ''', (user_id,))
                sid = cur.fetchone()[0]

                # Pernyataan yang sama dengan save_session_progress()
                end_time = "2026-01-01 08:00:15"
                t0 = time.perf_counter()
                cur.executemany('''
                    INSERT INTO drowsiness_events (session_id, event_type, event_time, duration)
                    VALUES (%s, %s, %s, %s)
                ''', [(sid, *e) for e in CONTOH_KEJADIAN])
                cur.execute(f'''
                    UPDATE driving_sessions s
                    SET end_time = %s,
                        duration = EXTRACT(EPOCH FROM (%s::timestamp - s.start_time::timestamp))::int,
                        {_TOTALS_FROM_EVENTS}
                    WHERE s.id = %s
                ''', (end_time, end_time, sid))
                t_tulis = (time.perf_counter() - t0) * 1000
            conn.rollback()  # tidak ada yang tersimpan
        finally:
            conn.close()
        rows.append({"ke": i, "handshake": t_handshake, "select": t_select, "tulis": t_tulis})

    return {"rows": rows, "n_sesi_akun": n_sesi}


def _ringkas(vals):
    return statistics.mean(vals), max(vals)


def format_tabel(hasil) -> str:
    rows = hasil["rows"]
    k = lambda x: f"{x:.1f}".replace(".", ",")
    hs = [r["handshake"] for r in rows]
    # Percobaan ke-1 ditandai kalau handshake-nya > 2x median percobaan lainnya
    # DAN lebih lambat dari semuanya (tanda compute Neon baru bangun dari
    # suspend / cold start). Syarat kedua perlu: variasi jaringan biasa bisa
    # membuat percobaan ke-1 > 2x median padahal percobaan lain sama lambatnya.
    cold = len(hs) > 1 and hs[0] > 2 * statistics.median(hs[1:]) and hs[0] > max(hs[1:])

    lines = [f"{'Percobaan':>9} | {'Handshake (ms)':>14} | {'SELECT (ms)':>11} | {'INSERT+UPDATE (ms)':>18}",
             "-" * 64]
    for r in rows:
        tanda = "  <- jauh lebih lambat (cold start?)" if (r["ke"] == 1 and cold) else ""
        lines.append(f"{r['ke']:>9} | {k(r['handshake']):>14} | {k(r['select']):>11} | {k(r['tulis']):>18}{tanda}")
    lines.append("-" * 64)
    for label, subset in (("Rata-rata", rows), ("Maksimum", rows)):
        f = statistics.mean if label == "Rata-rata" else max
        lines.append(f"{label:>9} | {k(f([r['handshake'] for r in subset])):>14} | "
                     f"{k(f([r['select'] for r in subset])):>11} | {k(f([r['tulis'] for r in subset])):>18}")
    if cold:
        sisa = rows[1:]
        lines.append(f"{'Rata2 2..'+str(len(rows)):>9} | {k(statistics.mean([r['handshake'] for r in sisa])):>14} | "
                     f"{k(statistics.mean([r['select'] for r in sisa])):>11} | "
                     f"{k(statistics.mean([r['tulis'] for r in sisa])):>18}   (tanpa percobaan ke-1)")
    lines.append("")
    lines.append(f"SELECT = query halaman Histori untuk 1 akun dengan {hasil['n_sesi_akun']} sesi.")
    lines.append("INSERT+UPDATE = 3 kejadian + update ringkasan sesi (seperti save_session_progress; "
                 "executemany = 1 perjalanan per baris), di-rollback; waktu commit tidak termasuk.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=10, help="jumlah percobaan (default 10)")
    ap.add_argument("--user", default=None, help="username untuk query Histori (default: akun dengan sesi terbanyak)")
    args = ap.parse_args()
    try:
        hasil = ukur(args.n, args.user)
    except Exception as e:
        raise SystemExit("Gagal mengukur: " + _aman(e))
    print(format_tabel(hasil))


if __name__ == "__main__":
    main()
