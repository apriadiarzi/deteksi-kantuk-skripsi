import cv2
import time
import numpy as np
import mediapipe as mp
from mediapipe.python.solutions.drawing_utils import _normalized_to_pixel_coordinates as denormalize_coordinates
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from decouple import config
import requests
import threading
from datetime import datetime
from database import now_wib
# database di-handle dari Streamlit saat Stop

def get_mediapipe_app(
    max_num_faces=1,
    refine_landmarks=False,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5,
):
    """Bangun FaceMesh.

    refine_landmarks=False disengaja. Flag itu menyalakan model iris TAMBAHAN
    yang jalan tiap frame (landmark 468-477), sementara semua indeks yang
    dipakai di sini — mata, mulut, kemiringan kepala — landmark standar semua.
    Jadi di versi sebelumnya model itu dihitung tiap frame untuk hasil yang
    tidak pernah dibaca; mahal sekali di CPU 1-core Streamlit Cloud.
    """
    face_mesh = mp.solutions.face_mesh.FaceMesh(
        max_num_faces=max_num_faces,
        refine_landmarks=refine_landmarks,
        min_detection_confidence=min_detection_confidence,
        min_tracking_confidence=min_tracking_confidence,
    )
    return face_mesh

def distance(point_1, point_2):
    dist = sum([(i - j) ** 2 for i, j in zip(point_1, point_2)]) ** 0.5
    return dist

# ─── EAR ──────────────────────────────────────────────────────────────────────

def get_ear(landmarks, refer_idxs, frame_width, frame_height):
    try:
        coords_points = []
        for i in refer_idxs:
            lm = landmarks[i]
            coord = denormalize_coordinates(lm.x, lm.y, frame_width, frame_height)
            coords_points.append(coord)

        P2_P6 = distance(coords_points[1], coords_points[5])
        P3_P5 = distance(coords_points[2], coords_points[4])
        P1_P4 = distance(coords_points[0], coords_points[3])
        ear = (P2_P6 + P3_P5) / (2.0 * P1_P4)
    except:
        ear = 0.0
        coords_points = None
    return ear, coords_points

def calculate_avg_ear(landmarks, left_eye_idxs, right_eye_idxs, image_w, image_h):
    left_ear, left_lm_coordinates  = get_ear(landmarks, left_eye_idxs,  image_w, image_h)
    right_ear, right_lm_coordinates = get_ear(landmarks, right_eye_idxs, image_w, image_h)
    Avg_EAR = (left_ear + right_ear) / 2.0
    return Avg_EAR, (left_lm_coordinates, right_lm_coordinates)

# ─── MAR ──────────────────────────────────────────────────────────────────────
# Threshold default 0.6 — di antara 0.5 (Albadawi et al., 2023) dan 0.65
# (Baul et al., 2025, MediaPipe Face Mesh). Catatan: rumus MAR tiap paper
# memakai titik bibir yang berbeda, jadi angkanya tidak bisa dibandingkan
# persis satu-satu — ini sebabnya threshold dibuat bisa diatur lewat slider.
#
#        P3(0)
#   P2(39)   P5(269)
# P1(61)         P4(291)
#   P8(146)  P6(375)
#        P7(17)
#
# MAR = (||P2-P8|| + ||P3-P7|| + ||P5-P6||) / (2 * ||P1-P4||)

MOUTH_IDXS = [61, 39, 0, 291, 269, 375, 17, 146]

# Minimum durasi event agar masuk database (detik). Fungsinya menyaring noise,
# bukan mendeteksi — jadi sengaja dibuat lebih panjang dari angka referensi.
#   Mata  : kedipan normal 100–400 ms, kedipan mengantuk ≥ 0,4–0,5 detik
#           (Albadawi et al., 2023). 1 detik pasti membuang kedipan normal.
#   Menguap: satu kali menguap berlangsung sekitar 4–6 detik (Albadawi et al.,
#           2023); Florez et al. (2024) mensyaratkan MAR terlampaui ≥ 5 detik.
#           1 detik cukup untuk tetap menangkap seluruh kejadian menguap.
#   Kepala: tidak ada referensi durasi; ditetapkan sendiri, sedikit lebih lama
#           dari mata/mulut karena kepala lebih sering bergerak sesaat
#           (menoleh ke spion, melihat dashboard).
MIN_DURATION_EYE  = 1.0
MIN_DURATION_YAWN = 1.0
MIN_DURATION_TILT = 1.5


def get_mar(landmarks, mouth_idxs, frame_width, frame_height):
    try:
        coords = []
        for i in mouth_idxs:
            lm = landmarks[i]
            coord = denormalize_coordinates(lm.x, lm.y, frame_width, frame_height)
            coords.append(coord)

        P2_P8 = distance(coords[1], coords[7])
        P3_P7 = distance(coords[2], coords[6])
        P5_P6 = distance(coords[4], coords[5])
        P1_P4 = distance(coords[0], coords[3])
        mar = (P2_P8 + P3_P7 + P5_P6) / (2.0 * P1_P4)
    except:
        mar = 0.0
        coords = None
    return mar, coords

# ─── Head Tilt (kemiringan kepala) ────────────────────────────────────────────
# Referensi threshold:
#   Baul et al. (2025) "Development of a Real-time Driver's Drowsiness
#   Detection System Using MediaPipe Face Mesh" — memakai ±20°, dengan
#   MediaPipe Face Mesh yang sama seperti sistem ini.
#   Default threshold: 20°
#
#   Sugeng & Nizar (2023) memakai batas yang jauh lebih longgar (> 45°)
#   dengan metode berbeda (garis dahi–dagu, bukan garis antar-telinga).
#
# Metode: hitung sudut kemiringan dari garis antara dua tragus telinga
#   Left ear tragus  = landmark 234
#   Right ear tragus = landmark 454
#   Sudut dihitung dari garis horizontal (atan2)

EAR_LEFT_TRAGUS  = 234
EAR_RIGHT_TRAGUS = 454

def get_head_tilt_angle(landmarks, frame_width, frame_height):
    """
    Hitung sudut kemiringan kepala dalam derajat.
    0° = tegak lurus, positif = miring kanan, negatif = miring kiri.
    Referensi: Baul et al. (2025)
    """
    try:
        left  = landmarks[EAR_LEFT_TRAGUS]
        right = landmarks[EAR_RIGHT_TRAGUS]

        lx = left.x  * frame_width
        ly = left.y  * frame_height
        rx = right.x * frame_width
        ry = right.y * frame_height

        dx = rx - lx
        dy = ry - ly
        angle = np.degrees(np.arctan2(dy, dx))  # sudut relatif horizontal
        return angle, (int(lx), int(ly)), (int(rx), int(ry))
    except:
        return 0.0, None, None

# ─── Plotting ─────────────────────────────────────────────────────────────────

def plot_eye_landmarks(frame, left_lm_coordinates, right_lm_coordinates, color):
    frame.flags.writeable = True
    for lm_coordinates in [left_lm_coordinates, right_lm_coordinates]:
        if lm_coordinates:
            for coord in lm_coordinates:
                cv2.circle(frame, coord, 2, color, -1)
    # ✅ Tidak di-flip — kamera tampil normal (bukan mirror)
    return frame

def plot_mouth_landmarks(frame, mouth_coords, color):
    """Gambar titik landmark mulut. Tidak perlu mirror karena frame tidak di-flip."""
    if mouth_coords:
        for coord in mouth_coords:
            if coord:
                cv2.circle(frame, coord, 2, color, -1)
    return frame

def plot_head_tilt(frame, left_pt, right_pt, angle, color):
    """Gambar garis dan sudut kemiringan kepala di frame."""
    if left_pt and right_pt:
        cv2.line(frame, left_pt, right_pt, color, 2)
        mid_x = (left_pt[0] + right_pt[0]) // 2
        mid_y = (left_pt[1] + right_pt[1]) // 2
        s = max(0.3, min(0.6, frame.shape[1] / 640 * 0.5))
        cv2.putText(frame, f"{angle:.1f}deg", (int(mid_x - 30 * s * 2), mid_y - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, s, color, 1)
    return frame

def plot_text(image, text, origin, color, font=cv2.FONT_HERSHEY_SIMPLEX, fntScale=None, thickness=None):
    # Ukuran huruf mengikuti lebar frame, bukan dipatok tetap. WebRTC
    # menurunkan resolusi sendiri saat CPU/bandwidth tertekan, dan ukuran tetap
    # bikin tulisannya menutupi wajah begitu frame-nya mengecil. Dikalibrasi
    # supaya frame selebar 640px tetap terlihat seperti sebelumnya (0.8).
    if fntScale is None:
        fntScale = max(0.35, min(1.0, image.shape[1] / 640 * 0.8))
    if thickness is None:
        thickness = max(1, round(fntScale * 2.5))
    image = cv2.putText(image, text, origin, font, fntScale, color, thickness)
    return image

# ─── Low-light Enhancement ────────────────────────────────────────────────────
# Referensi: Aprilia & Fitrianah (2025) "Automatic drowsiness detection system
# to reduce road accident risks" — CLAHE untuk deteksi kantuk di kondisi minim
# cahaya. Mereka memakai clip limit 2.0 dengan tile 8×8; di sini clip limit-nya
# 3.0 (kontras lebih kuat), tile-nya sama.

# Objek CLAHE dibuat sekali di sini, bukan di dalam enhance_low_light().
# Parameternya tidak pernah berubah. Diukur, penghematannya ternyata kecil
# sekali (createCLAHE memang murah) — ini dibiarkan karena lebih rapi, bukan
# karena kencang.
#
# Sempat dicoba MELEWATI CLAHE untuk frame yang sudah terang, lalu dibatalkan:
# seluruh enhance_low_light() cuma ~1ms dari ~5ms per frame, jadi tidak ada
# yang berarti untuk dihemat — sementara melewatinya akan mengubah metodologi
# CLAHE yang dipakai di skripsi, tergantung terang-gelapnya ruangan.
_CLAHE = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))

def enhance_low_light(frame):
    """
    Tingkatkan kecerahan frame di kondisi minim cahaya menggunakan CLAHE.
    Hanya channel luminance (Y) yang diproses agar warna tidak berubah drastis.
    """
    yuv = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV)
    yuv[:, :, 0] = _CLAHE.apply(yuv[:, :, 0])
    return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)


# ─── Pembatasan email darurat ─────────────────────────────────────────────────
# Paling banyak SATU email per jendela ini, untuk semua jenis kejadian sekaligus
# (mata tertutup, menguap, kepala miring). Kejadian pertama langsung dikirim;
# kejadian lain di dalam jendela tidak dikirim, tapi DIHITUNG, lalu dilaporkan
# sebagai satu email ringkasan begitu jendelanya habis. Jadi penerima tetap
# tahu pengemudinya berulang kali mengantuk — tanpa kotak masuknya dibanjiri.
EMAIL_COOLDOWN_SECONDS = 2 * 60

EVENT_LABELS = {
    "eye_close": "mata tertutup",
    "yawn":      "menguap",
    "head_tilt": "kepala miring",
}

# ─── Main Handler ─────────────────────────────────────────────────────────────

class VideoFrameHandler:
    def __init__(self):
        self.api_key        = config('GOOGLE_API_KEY')
        self.email_sender   = "deteksikantuk@gmail.com"
        self.email_password = config('EMAIL_PASSWORD', default='')
        self.email_recipients = []  # diisi dari Streamlit sesuai email akun yang login; kosong = tidak kirim (mis. tamu)
        # Link Google Maps posisi terakhir pengemudi, diisi dari Streamlit.
        # Disimpan per handler (= per sesi browser). Dulu ini satu variabel
        # global di modul, yang dipakai bersama SEMUA pengguna di server —
        # email darurat pengguna A bisa berisi lokasi pengguna B.
        self.location_link = "Lokasi tidak tersedia"

        # Status pembatasan email (lihat EMAIL_COOLDOWN_SECONDS). Disimpan di
        # handler, yang hidup di session_state, jadi jedanya tetap berlaku
        # walau kamera di-STOP lalu START lagi.
        self._last_email_at  = None  # time.monotonic() saat email terakhir dikirim
        self._held_events    = {}    # jenis kejadian -> jumlah yang ditahan

        self.eye_idxs = {
            "left":  [362, 385, 387, 263, 373, 380],
            "right": [33, 160, 158, 133, 153, 144],
        }
        self.mouth_idxs = MOUTH_IDXS

        self.RED    = (0, 0, 255)
        self.GREEN  = (0, 255, 0)
        self.ORANGE = (0, 165, 255)
        self.YELLOW = (0, 255, 255)

        self.facemesh_model = get_mediapipe_app()

        # Buffer event sementara — di-push ke DB saat user klik Stop
        self.pending_events    = []
        self._eye_event_start  = None
        self._yawn_event_start = None
        self._tilt_event_start = None

        self.state_tracker = {
            # EAR
            "start_time":    time.perf_counter(),
            "DROWSY_TIME":   0.0,
            "COLOR":         self.GREEN,
            "play_alarm":    False,
            "message_sent":  False,
            # MAR
            "YAWN_start_time":    time.perf_counter(),
            "YAWN_TIME":          0.0,
            "is_yawning":         False,
            "yawn_alarm":         False,
            "yawn_message_sent":  False,
            # Head tilt
            "TILT_start_time":    time.perf_counter(),
            "TILT_TIME":          0.0,
            "is_tilting":         False,
            "tilt_alarm":         False,
            "tilt_message_sent":  False,
        }
        self.EAR_txt_pos = (10, 30)
        self.MAR_txt_pos = (10, 60)
        self.TILT_txt_pos = (10, 90)

    def _send_email_worker(self, subject: str, message: str):
        """Background thread — tidak membekukan video."""
        try:
            full_message  = f"{message}\n\nLokasi pengguna saat ini: {self.location_link}"

            msg = MIMEMultipart()
            msg["From"]    = self.email_sender
            msg["Subject"] = subject
            msg.attach(MIMEText(full_message, "plain"))

            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.starttls()
                server.login(self.email_sender, self.email_password)
                for recipient in self.email_recipients:
                    # `msg["To"] = ...` MENAMBAH header, bukan mengganti — tanpa
                    # del, penerima kedua ikut melihat alamat penerima pertama.
                    del msg["To"]
                    msg["To"] = recipient
                    server.sendmail(self.email_sender, recipient, msg.as_string())
        except Exception as e:
            print(f"Gagal mengirim email: {e}")

    def send_email_alert(self, subject: str, message: str):
        t = threading.Thread(target=self._send_email_worker, args=(subject, message), daemon=True)
        t.start()

    def _email_window_open(self) -> bool:
        return (self._last_email_at is None
                or time.monotonic() - self._last_email_at >= EMAIL_COOLDOWN_SECONDS)

    def request_email_alert(self, kind: str, subject: str, message: str):
        """Minta email darurat untuk satu kejadian, lewat pembatas.

        Kalau jendela jeda sudah lewat, email langsung dikirim. Kalau belum,
        kejadiannya cuma dihitung — nanti dilaporkan oleh flush_held_emails().
        """
        if not self.email_recipients:
            return  # tamu / akun tanpa email: tidak ada yang perlu dikirimi
        if self._email_window_open():
            self._last_email_at = time.monotonic()
            self.send_email_alert(subject, message)
        else:
            self._held_events[kind] = self._held_events.get(kind, 0) + 1

    def flush_held_emails(self):
        """Kirim SATU email ringkasan untuk kejadian yang ditahan, begitu
        jendela jedanya habis. Dipanggil tiap frame dari process()."""
        if not self._held_events or not self._email_window_open():
            return
        rincian = ", ".join(
            f"{jumlah}× {EVENT_LABELS.get(jenis, jenis)}"
            for jenis, jumlah in self._held_events.items()
        )
        menit = EMAIL_COOLDOWN_SECONDS // 60
        self._held_events.clear()
        self._last_email_at = time.monotonic()
        self.send_email_alert(
            "⚠️ Pengemudi Masih Terdeteksi Mengantuk",
            f"Dalam {menit} menit terakhir sejak email sebelumnya, sistem kembali "
            f"mendeteksi tanda kantuk pada pengemudi: {rincian}.\n\n"
            "Kejadian yang berulang menandakan pengemudi masih mengantuk. Mohon "
            "segera hubungi pengemudi dan sarankan untuk menepi dan beristirahat "
            "di rest area terdekat.\n\n"
            f"Untuk mencegah spam, email dari sistem ini dikirim paling banyak "
            f"satu kali setiap {menit} menit.\n\n"
            "Pesan ini dikirim otomatis oleh Sistem Pendeteksi Kantuk."
        )

    def process(self, frame: np.array, thresholds: dict):
        frame_h, frame_w, _ = frame.shape

        # Dicek di awal, sebelum deteksi wajah — kalau kepala pengemudi sampai
        # terkulai dan wajahnya tidak terdeteksi, ringkasan tetap harus terkirim.
        self.flush_held_emails()

        # Enhance frame untuk kondisi minim cahaya (CLAHE)
        frame = enhance_low_light(frame)

        # Jarak antar baris ikut lebar frame, sejalan dengan ukuran huruf di
        # plot_text() — kalau dipatok tetap, teksnya jadi terlalu renggang di
        # frame kecil dan terlalu rapat di frame besar.
        ui = max(0.45, min(1.25, frame_w / 640))
        pad, line = int(10 * ui), int(30 * ui)

        self.EAR_txt_pos  = (pad, line)
        self.MAR_txt_pos  = (pad, line * 2)
        self.TILT_txt_pos = (pad, line * 3)

        DROWSY_TIME_txt_pos = (pad, int(frame_h - line * 3))
        YAWN_TIME_txt_pos   = (pad, int(frame_h - line * 2))
        TILT_TIME_txt_pos   = (pad, int(frame_h - line))
        WARN_DROWSY_pos = (pad, int(frame_h / 2 - line * 0.7))
        WARN_YAWN_pos   = (pad, int(frame_h / 2 + line * 0.7))
        WARN_TILT_pos   = (pad, int(frame_h / 2 + line * 2))
        ALM_txt_pos     = (pad, int(frame_h / 2 - line * 2))

        # MediaPipe mengharapkan RGB. Sebelumnya frame BGR dikirim apa adanya,
        # jadi channel merah dan biru tertukar. Pada gambar uji di repo wajahnya
        # ternyata TETAP kedeteksi walau formatnya salah, jadi ini bukan
        # perbaikan kecepatan — ini soal benar: model dilatih pada RGB, dan
        # presisi landmark pada wajah/pencahayaan lain tidak bisa diandalkan
        # kalau inputnya tertukar begini.
        #
        # Salinan RGB-nya ditandai read-only supaya MediaPipe boleh memakainya
        # tanpa menyalin lagi; `frame` sendiri tetap BGR dan tetap bisa ditulisi
        # oleh fungsi-fungsi plot_* di bawah.
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False
        results = self.facemesh_model.process(rgb)

        if results.multi_face_landmarks:
            landmarks = results.multi_face_landmarks[0].landmark

            # ── EAR ──────────────────────────────────────────────────────────
            EAR, eye_coords = calculate_avg_ear(
                landmarks, self.eye_idxs["left"], self.eye_idxs["right"], frame_w, frame_h
            )
            frame = plot_eye_landmarks(frame, eye_coords[0], eye_coords[1], self.state_tracker["COLOR"])

            # ── MAR ──────────────────────────────────────────────────────────
            MAR, mouth_coords = get_mar(landmarks, self.mouth_idxs, frame_w, frame_h)
            frame = plot_mouth_landmarks(frame, mouth_coords, self.ORANGE)

            # ── Head Tilt ─────────────────────────────────────────────────────
            tilt_angle, left_pt, right_pt = get_head_tilt_angle(landmarks, frame_w, frame_h)
            abs_angle = abs(tilt_angle)
            tilt_color = self.YELLOW if abs_angle > thresholds["TILT_THRESH"] else self.GREEN
            frame = plot_head_tilt(frame, left_pt, right_pt, tilt_angle, tilt_color)

            # ── Logika EAR ────────────────────────────────────────────────────
            if EAR < thresholds["EAR_THRESH"]:
                if self._eye_event_start is None:
                    self._eye_event_start = now_wib()
                end_time = time.perf_counter()
                self.state_tracker["DROWSY_TIME"] += end_time - self.state_tracker["start_time"]
                self.state_tracker["start_time"]   = end_time
                self.state_tracker["COLOR"]        = self.RED

                if self.state_tracker["DROWSY_TIME"] >= thresholds["WAIT_TIME"]:
                    self.state_tracker["play_alarm"] = True
                    plot_text(frame, "WAKE UP! WAKE UP", ALM_txt_pos, self.RED)
                    plot_text(frame, "MATA TERTUTUP!", WARN_DROWSY_pos, self.RED)

                    if not self.state_tracker["message_sent"] and self.state_tracker["DROWSY_TIME"] >= 5:
                        self.request_email_alert(
                            "eye_close",
                            "⚠️ Pengemudi Terdeteksi Mengantuk!",
                            "Kami mendeteksi bahwa mata pengemudi tertutup selama "
                            "lebih dari 5 detik saat berkendara. Kondisi ini merupakan tanda "
                            "bahwa pengemudi sedang tertidur atau sangat mengantuk, dan "
                            "sangat berbahaya jika dibiarkan.\n\n"
                            "Mohon segera hubungi pengemudi dan sarankan untuk segera menepi "
                            "dan beristirahat di rest area terdekat.\n\n"
                            "Pesan ini dikirim otomatis oleh Sistem Pendeteksi Kantuk."
                        )
                        self.state_tracker["message_sent"] = True
            else:
                if self._eye_event_start is not None:
                    duration = self.state_tracker["DROWSY_TIME"]
                    if duration >= MIN_DURATION_EYE:  # filter noise < 1 detik
                        self.pending_events.append({
                            'event_type': 'eye_close',
                            'event_time': self._eye_event_start.strftime('%Y-%m-%d %H:%M:%S'),
                            'duration':   round(duration, 2)
                        })
                    self._eye_event_start = None
                self.state_tracker["start_time"]   = time.perf_counter()
                self.state_tracker["DROWSY_TIME"]   = 0.0
                self.state_tracker["COLOR"]         = self.GREEN
                self.state_tracker["play_alarm"]    = False
                self.state_tracker["message_sent"]  = False

            # ── Logika MAR ────────────────────────────────────────────────────
            if MAR > thresholds["MAR_THRESH"]:
                if self._yawn_event_start is None:
                    self._yawn_event_start = now_wib()
                yawn_end = time.perf_counter()
                self.state_tracker["YAWN_TIME"]       += yawn_end - self.state_tracker["YAWN_start_time"]
                self.state_tracker["YAWN_start_time"]  = yawn_end
                self.state_tracker["is_yawning"]       = True
                # Alarm baru menyala setelah mulut terbuka selama WAIT_TIME,
                # sama seperti mata. Dulu langsung menyala begitu MAR lewat
                # ambang di SATU frame saja — bicara sebentar pun membunyikan
                # alarm.
                if self.state_tracker["YAWN_TIME"] >= thresholds["WAIT_TIME"]:
                    self.state_tracker["yawn_alarm"] = True
                    plot_text(frame, "MENGUAP TERDETEKSI!", WARN_YAWN_pos, self.ORANGE)

                if not self.state_tracker["yawn_message_sent"] and self.state_tracker["YAWN_TIME"] >= 3:
                    self.request_email_alert(
                        "yawn",
                        "⚠️ Pengemudi Terdeteksi Mengantuk!",
                        "Kami mendeteksi bahwa pengemudi telah menguap selama "
                        "lebih dari 3 detik saat berkendara. Menguap berlebihan merupakan "
                        "tanda awal rasa kantuk yang dapat membahayakan keselamatan "
                        "berkendara.\n\n"
                        "Mohon segera hubungi pengemudi dan sarankan untuk beristirahat "
                        "di rest area terdekat.\n\n"
                        "Pesan ini dikirim otomatis oleh Sistem Pendeteksi Kantuk."
                    )
                    self.state_tracker["yawn_message_sent"] = True
            else:
                if self._yawn_event_start is not None:
                    duration = self.state_tracker["YAWN_TIME"]
                    if duration >= MIN_DURATION_YAWN:  # filter noise < 1 detik
                        self.pending_events.append({
                            'event_type': 'yawn',
                            'event_time': self._yawn_event_start.strftime('%Y-%m-%d %H:%M:%S'),
                            'duration':   round(duration, 2)
                        })
                    self._yawn_event_start = None
                self.state_tracker["YAWN_start_time"]   = time.perf_counter()
                self.state_tracker["YAWN_TIME"]          = 0.0
                self.state_tracker["is_yawning"]         = False
                self.state_tracker["yawn_alarm"]         = False
                self.state_tracker["yawn_message_sent"]  = False

            # ── Logika Head Tilt ──────────────────────────────────────────────
            if abs_angle > thresholds["TILT_THRESH"]:
                if self._tilt_event_start is None:
                    self._tilt_event_start = now_wib()
                tilt_end = time.perf_counter()
                self.state_tracker["TILT_TIME"]       += tilt_end - self.state_tracker["TILT_start_time"]
                self.state_tracker["TILT_start_time"]  = tilt_end
                self.state_tracker["is_tilting"]       = True
                # Sama seperti menguap: tunggu WAIT_TIME dulu, supaya kepala
                # yang miring sesaat (menoleh ke spion, dsb.) tidak membunyikan
                # alarm.
                if self.state_tracker["TILT_TIME"] >= thresholds["WAIT_TIME"]:
                    self.state_tracker["tilt_alarm"] = True
                    plot_text(frame, "KEPALA MIRING!", WARN_TILT_pos, self.YELLOW)

                if not self.state_tracker["tilt_message_sent"] and self.state_tracker["TILT_TIME"] >= 5:
                    self.request_email_alert(
                        "head_tilt",
                        "⚠️ Pengemudi Terdeteksi Mengantuk!",
                        f"Kami mendeteksi bahwa kepala pengemudi miring ke samping "
                        f"selama lebih dari 5 detik saat berkendara. Kondisi ini biasanya "
                        f"terjadi ketika pengemudi tertidur atau hampir tertidur, dan sangat "
                        f"berbahaya jika dibiarkan.\n\n"
                        f"Mohon segera hubungi pengemudi dan sarankan untuk segera menepi "
                        f"dan beristirahat di rest area terdekat.\n\n"
                        f"Pesan ini dikirim otomatis oleh Sistem Pendeteksi Kantuk."
                    )
                    self.state_tracker["tilt_message_sent"] = True
            else:
                if self._tilt_event_start is not None:
                    duration = self.state_tracker["TILT_TIME"]
                    if duration >= MIN_DURATION_TILT:  # filter noise < 1.5 detik
                        self.pending_events.append({
                            'event_type': 'head_tilt',
                            'event_time': self._tilt_event_start.strftime('%Y-%m-%d %H:%M:%S'),
                            'duration':   round(duration, 2)
                        })
                    self._tilt_event_start = None
                self.state_tracker["TILT_start_time"]   = time.perf_counter()
                self.state_tracker["TILT_TIME"]          = 0.0
                self.state_tracker["is_tilting"]         = False
                self.state_tracker["tilt_alarm"]         = False
                self.state_tracker["tilt_message_sent"]  = False

            # ── Teks overlay ──────────────────────────────────────────────────
            plot_text(frame, f"EAR: {round(EAR, 2)}",       self.EAR_txt_pos,  self.state_tracker["COLOR"])
            plot_text(frame, f"MAR: {round(MAR, 2)}",       self.MAR_txt_pos,  self.ORANGE if self.state_tracker["is_yawning"] else self.GREEN)
            plot_text(frame, f"TILT: {tilt_angle:.1f}deg",  self.TILT_txt_pos, tilt_color)
            plot_text(frame, f"DROWSY: {round(self.state_tracker['DROWSY_TIME'], 3)} Secs", DROWSY_TIME_txt_pos, self.state_tracker["COLOR"])
            plot_text(frame, f"YAWN  : {round(self.state_tracker['YAWN_TIME'], 3)} Secs",   YAWN_TIME_txt_pos,   self.ORANGE if self.state_tracker["is_yawning"] else self.GREEN)
            plot_text(frame, f"TILT  : {round(self.state_tracker['TILT_TIME'], 3)} Secs",   TILT_TIME_txt_pos,   tilt_color)

        else:
            self.state_tracker.update({
                "start_time": time.perf_counter(), "DROWSY_TIME": 0.0,
                "COLOR": self.GREEN, "play_alarm": False,
                "YAWN_start_time": time.perf_counter(), "YAWN_TIME": 0.0,
                "is_yawning": False, "yawn_alarm": False,
                "TILT_start_time": time.perf_counter(), "TILT_TIME": 0.0,
                "is_tilting": False, "tilt_alarm": False,
            })

        play_alarm = (
            self.state_tracker["play_alarm"] or
            self.state_tracker["yawn_alarm"] or
            self.state_tracker["tilt_alarm"]
        )
        return frame, play_alarm
