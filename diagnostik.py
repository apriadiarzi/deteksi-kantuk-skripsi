"""Pengukuran waktu proses per frame, khusus mode diagnostik.

Aktif hanya kalau DIAGNOSTIK=1 — lewat Secrets di Streamlit Cloud (kunci di
level teratas, Streamlit menjadikannya environment variable saat server
start) atau lewat .env / environment variable di lokal. Saat mati, aplikasi
berjalan persis seperti biasa: tidak ada yang diukur, dicatat, atau ditampilkan.
"""
import math
import threading

from decouple import config

DIAGNOSTIK_AKTIF = config("DIAGNOSTIK", default="0").strip().lower() in ("1", "true", "yes", "on")


class FrameTimer:
    """Kumpulan waktu proses per frame yang aman dipakai lintas thread.

    record() dipanggil dari thread kamera (callback WebRTC), summary() dari
    thread script Streamlit — keduanya dilindungi satu lock.
    """

    WARMUP_FRAMES = 10   # frame pertama setelah START tidak dihitung (pemanasan)
    LOG_EVERY = 100      # ringkasan dicetak ke console tiap sekian frame terhitung

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        """Mulai pengukuran baru — dipanggil tiap sesi kamera dimulai."""
        with self._lock:
            self._seen = 0         # semua frame, termasuk pemanasan
            self._samples = []     # ms, hanya frame setelah pemanasan
            self._sum = 0.0
            self._max = 0.0

    def record(self, ms: float):
        with self._lock:
            self._seen += 1
            if self._seen <= self.WARMUP_FRAMES:
                return
            self._samples.append(ms)
            self._sum += ms
            self._max = max(self._max, ms)
            n = len(self._samples)
            line = None
            if n % self.LOG_EVERY == 0:
                line = (f"[DIAGNOSTIK] waktu proses frame: n={n} "
                        f"rata-rata={self._sum / n:.1f} ms maks={self._max:.1f} ms")
        if line:
            print(line, flush=True)  # di luar lock: print bisa lambat

    def summary(self):
        """dict ringkasan, atau None kalau belum ada frame terhitung."""
        with self._lock:
            data = list(self._samples)
            seen = self._seen
        if not data:
            return None
        data.sort()
        n = len(data)

        def persentil(p):
            # metode nearest-rank: nilai yang benar-benar terukur, tanpa interpolasi
            return data[max(0, math.ceil(p / 100 * n) - 1)]

        return {
            "n": n,
            "diabaikan": min(seen, self.WARMUP_FRAMES),
            "rata_rata": sum(data) / n,
            "median": persentil(50),
            "p95": persentil(95),
            "min": data[0],
            "maks": data[-1],
        }
