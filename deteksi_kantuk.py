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

# ── Cache lokasi dari browser ─────────────────────────────────────────────────
_location_cache = {"maps_link": "Lokasi tidak tersedia"}

def set_location_cache(maps_link: str):
    _location_cache["maps_link"] = maps_link

def get_current_location(api_key=None):
    return _location_cache["maps_link"]

def get_mediapipe_app(
    max_num_faces=1,
    refine_landmarks=True,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5,
):
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
# Threshold default 0.6 — Mandal et al. (2017) & Vural et al. (2007)
#
#        P3(0)
#   P2(39)   P5(269)
# P1(61)         P4(291)
#   P8(146)  P6(375)
#        P7(17)
#
# MAR = (||P2-P8|| + ||P3-P7|| + ||P5-P6||) / (2 * ||P1-P4||)

MOUTH_IDXS = [61, 39, 0, 291, 269, 375, 17, 146]

# Minimum durasi event agar masuk database (detik)
MIN_DURATION_EYE  = 1.0   # Soukupová & Čech (2016)
MIN_DURATION_YAWN = 1.0   # Vural et al. (2007)
MIN_DURATION_TILT = 1.5   # Sahayadhas et al. (2012)


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
#   Sahayadhas et al. (2012) "Detecting Driver Drowsiness Based on Sensors"
#   Mbouna et al. (2013) "Visual Analysis of Eye State and Head Pose for Driver Alertness Monitoring"
#   → Kemiringan kepala > 15° dianggap tanda kantuk ringan
#   → Kemiringan kepala > 25°–30° dianggap tanda kantuk berat
#   Default threshold: 20° (tengah antara dua referensi)
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
    Referensi: Sahayadhas et al. (2012), Mbouna et al. (2013)
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
# Referensi: Reza (2004) "Realization of the Contrast Limited Adaptive
# Histogram Equalization (CLAHE) for Real-Time Image Enhancement"

def enhance_low_light(frame):
    """
    Tingkatkan kecerahan frame di kondisi minim cahaya menggunakan CLAHE.
    Hanya channel luminance (Y) yang diproses agar warna tidak berubah drastis.
    """
    yuv = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    yuv[:, :, 0] = clahe.apply(yuv[:, :, 0])
    return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)


# ─── Main Handler ─────────────────────────────────────────────────────────────

class VideoFrameHandler:
    def __init__(self):
        self.api_key        = config('GOOGLE_API_KEY')
        self.email_sender   = "deteksikantuk@gmail.com"
        self.email_password = config('EMAIL_PASSWORD', default='')
        self.email_recipients = []  # diisi dari Streamlit sesuai email akun yang login; kosong = tidak kirim (mis. tamu)

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
            location_link = get_current_location()
            full_message  = f"{message}\n\nLokasi pengguna saat ini: {location_link}"

            msg = MIMEMultipart()
            msg["From"]    = self.email_sender
            msg["Subject"] = subject
            msg.attach(MIMEText(full_message, "plain"))

            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.starttls()
                server.login(self.email_sender, self.email_password)
                for recipient in self.email_recipients:
                    msg["To"] = recipient
                    server.sendmail(self.email_sender, recipient, msg.as_string())
                    print(f"Email terkirim ke {recipient}")
        except Exception as e:
            print(f"Gagal mengirim email: {e}")

    def send_email_alert(self, subject: str, message: str):
        t = threading.Thread(target=self._send_email_worker, args=(subject, message), daemon=True)
        t.start()

    def process(self, frame: np.array, thresholds: dict):
        frame.flags.writeable = False
        frame_h, frame_w, _ = frame.shape

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

        results = self.facemesh_model.process(frame)
        frame.flags.writeable = True

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
                        self.send_email_alert(
                            "⚠️ Pengemudi Terdeteksi Mengantuk!",
                            "Halo,\n\nKami mendeteksi bahwa mata pengemudi tertutup selama "
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
                self.state_tracker["yawn_alarm"]       = True
                plot_text(frame, "MENGUAP TERDETEKSI!", WARN_YAWN_pos, self.ORANGE)

                if not self.state_tracker["yawn_message_sent"] and self.state_tracker["YAWN_TIME"] >= 3:
                    self.send_email_alert(
                        "⚠️ Pengemudi Terdeteksi Mengantuk!",
                        "Halo,\n\nKami mendeteksi bahwa pengemudi telah menguap selama "
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
                self.state_tracker["tilt_alarm"]       = True
                plot_text(frame, "KEPALA MIRING!", WARN_TILT_pos, self.YELLOW)

                if not self.state_tracker["tilt_message_sent"] and self.state_tracker["TILT_TIME"] >= 5:
                    self.send_email_alert(
                        "⚠️ Pengemudi Terdeteksi Mengantuk!",
                        f"Halo,\n\nKami mendeteksi bahwa kepala pengemudi miring ke samping "
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
