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

# Fungsi untuk mendapatkan lokasi menggunakan Google Geolocation API
def get_current_location(api_key):
    try:
        # Data untuk request
        data = {
            "considerIp": True
        }
        headers = {
            "Content-Type": "application/json"
        }

        # Kirim request ke Google Geolocation API
        response = requests.post(
            f"https://www.googleapis.com/geolocation/v1/geolocate?key={api_key}",
            json=data,
            headers=headers
        )
        response_data = response.json()

        # Ambil latitude dan longitude dari response
        if "location" in response_data:
            latitude = response_data["location"]["lat"]
            longitude = response_data["location"]["lng"]
            maps_link = f"https://www.google.com/maps?q={latitude},{longitude}"
            return maps_link
        else:
            return "Lokasi tidak dapat diambil."
    except Exception as e:
        print(f"Error saat mengambil lokasi: {e}")
        return "Lokasi tidak dapat diambil."

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
    left_ear, left_lm_coordinates = get_ear(landmarks, left_eye_idxs, image_w, image_h)
    right_ear, right_lm_coordinates = get_ear(landmarks, right_eye_idxs, image_w, image_h)
    Avg_EAR = (left_ear + right_ear) / 2.0
    return Avg_EAR, (left_lm_coordinates, right_lm_coordinates)

def plot_eye_landmarks(frame, left_lm_coordinates, right_lm_coordinates, color):
    frame.flags.writeable = True
    for lm_coordinates in [left_lm_coordinates, right_lm_coordinates]:
        if lm_coordinates:
            for coord in lm_coordinates:
                cv2.circle(frame, coord, 2, color, -1)
    frame = cv2.flip(frame, 1)
    return frame

def plot_text(image, text, origin, color, font=cv2.FONT_HERSHEY_SIMPLEX, fntScale=0.8, thickness=2):
    image = cv2.putText(image, text, origin, font, fntScale, color, thickness)
    return image

class VideoFrameHandler:
    def __init__(self):
        self.api_key = config('GOOGLE_API_KEY')
        self.email_sender = "deteksikantuk@gmail.com"  # Ganti dengan email Anda
        self.email_password = "loqzsyuhtrbllspw"  # Ganti dengan password aplikasi
        self.email_recipients = ["apriadiarzi22@gmail.com"]
        self.eye_idxs = {
            "left": [362, 385, 387, 263, 373, 380],
            "right": [33, 160, 158, 133, 153, 144],
        }
        self.RED = (0, 0, 255)
        self.GREEN = (0, 255, 0)
        self.facemesh_model = get_mediapipe_app()
        self.state_tracker = {
            "start_time": time.perf_counter(),
            "DROWSY_TIME": 0.0,
            "COLOR": self.GREEN,
            "play_alarm": False,
            "message_sent": False,
        }
        self.EAR_txt_pos = (10, 30)

    def send_email_alert(self, subject: str, message: str):
        try:
            msg = MIMEMultipart()
            msg['From'] = self.email_sender
            msg['Subject'] = subject

            # Tambahkan tautan lokasi ke dalam email
            location_link = get_current_location(self.api_key)
            full_message = f"{message}\n\nLokasi pengguna saat ini: {location_link}"

            msg.attach(MIMEText(full_message, 'plain'))

            with smtplib.SMTP('smtp.gmail.com', 587) as server:
                server.starttls()
                server.login(self.email_sender, self.email_password)
                for recipient in self.email_recipients:
                    msg['To'] = recipient
                    server.sendmail(self.email_sender, recipient, msg.as_string())
                    print(f"Email terkirim ke {recipient}: {full_message}")
        except Exception as e:
            print(f"Gagal mengirim email: {e}")

    def process(self, frame: np.array, thresholds: dict):
        frame.flags.writeable = False
        frame_h, frame_w, _ = frame.shape

        DROWSY_TIME_txt_pos = (10, int(frame_h // 2 * 1.7))
        ALM_txt_pos = (10, int(frame_h // 2 * 1.85))

        results = self.facemesh_model.process(frame)

        if results.multi_face_landmarks:
            landmarks = results.multi_face_landmarks[0].landmark
            EAR, coordinates = calculate_avg_ear(landmarks, self.eye_idxs["left"], self.eye_idxs["right"], frame_w, frame_h)
            frame = plot_eye_landmarks(frame, coordinates[0], coordinates[1], self.state_tracker["COLOR"])

            if EAR < thresholds["EAR_THRESH"]:
                end_time = time.perf_counter()
                self.state_tracker["DROWSY_TIME"] += end_time - self.state_tracker["start_time"]
                self.state_tracker["start_time"] = end_time
                self.state_tracker["COLOR"] = self.RED

                if self.state_tracker["DROWSY_TIME"] >= thresholds["WAIT_TIME"]:
                    self.state_tracker["play_alarm"] = True
                    plot_text(frame, "WAKE UP! WAKE UP", ALM_txt_pos, self.state_tracker["COLOR"])

                    if not self.state_tracker["message_sent"] and self.state_tracker["DROWSY_TIME"] >= 5:
                        self.send_email_alert(
                            "Peringatan Drowsiness!",
                            "Pengguna telah tertidur selama lebih dari 5 detik. Harap segera lakukan tindakan!"
                        )
                        self.state_tracker["message_sent"] = True
            else:
                self.state_tracker["start_time"] = time.perf_counter()
                self.state_tracker["DROWSY_TIME"] = 0.0
                self.state_tracker["COLOR"] = self.GREEN
                self.state_tracker["play_alarm"] = False
                self.state_tracker["message_sent"] = False

            EAR_txt = f"EAR: {round(EAR, 2)}"
            DROWSY_TIME_txt = f"DROWSY: {round(self.state_tracker['DROWSY_TIME'], 3)} Secs"
            plot_text(frame, EAR_txt, self.EAR_txt_pos, self.state_tracker["COLOR"])
            plot_text(frame, DROWSY_TIME_txt, DROWSY_TIME_txt_pos, self.state_tracker["COLOR"])

        else:
            self.state_tracker["start_time"] = time.perf_counter()
            self.state_tracker["DROWSY_TIME"] = 0.0
            self.state_tracker["COLOR"] = self.GREEN
            self.state_tracker["play_alarm"] = False

        return frame, self.state_tracker["play_alarm"]

# Tes fungsi pengambilan lokasi
if __name__ == "__main__":
    API_KEY = config('GOOGLE_API_KEY')
    handler = VideoFrameHandler()
    lokasi = get_current_location(API_KEY)
    print(f"Lokasi Google Maps: {lokasi}")
    handler.send_email_alert("Peringatan Drowsiness!", "Pengguna telah tertidur selama lebih dari 5 detik.")