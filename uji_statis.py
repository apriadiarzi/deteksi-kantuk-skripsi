"""
Uji statis (Subbab 4.3.1 Verifikasi Perhitungan pada Citra Statis).

Taruh file ini di folder project (sejajar dengan deteksi_kantuk.py), lalu jalankan:
    python uji_statis.py

Pengaturan sama dengan aplikasi:
  - fungsi EAR, MAR, sudut kepala, CLAHE diambil langsung dari deteksi_kantuk.py
  - nomor & urutan titik landmark sama dengan VideoFrameHandler
  - CLAHE (clipLimit 3,0, tile 8x8) pada kanal Y, konversi BGR -> RGB
  - FaceMesh: 1 wajah, refine_landmarks=False, confidence 0,5
  - threshold bawaan aplikasi: EAR 0,23 | MAR 0,60 | sudut 20 derajat

Hasil: folder "hasil_uji_statis" berisi gambar beranotasi + hasil_uji_statis.csv
"""
import os
import sys
import csv

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)  # supaya .env project terbaca oleh deteksi_kantuk/database

from deteksi_kantuk import (  # noqa: E402  (fungsi asli aplikasi)
    get_mediapipe_app, calculate_avg_ear, get_mar, get_head_tilt_angle,
    enhance_low_light, plot_eye_landmarks, plot_mouth_landmarks,
    plot_head_tilt, plot_text, MOUTH_IDXS,
)

# Sama persis dengan VideoFrameHandler.__init__ di deteksi_kantuk.py
EYE_IDXS = {
    "left":  [362, 385, 387, 263, 373, 380],
    "right": [33, 160, 158, 133, 153, 144],
}
RED, GREEN, ORANGE, YELLOW = (0, 0, 255), (0, 255, 0), (0, 165, 255), (0, 255, 255)

# Nilai bawaan slider di aplikasi
EAR_THRESH, MAR_THRESH, TILT_THRESH = 0.23, 0.60, 20.0

FILES = [
    ("test-open-eyes.jpg",   "mata terbuka"),
    ("test-open-eyes2.jpg",  "mata terbuka"),
    ("test-close-eyes.jpg",  "mata tertutup"),
    ("test-close-eyes2.jpg", "mata tertutup"),
    ("test-yawning.jpg",     "menguap"),
    ("test-head-tilt.jpeg",  "kepala miring"),
]
OUT_DIR = os.path.join(HERE, "hasil_uji_statis")
MIN_FIG_WIDTH = 640  # gambar kecil diperbesar HANYA untuk tampilan, bukan untuk perhitungan


def koma(x, n=3):
    return f"{x:.{n}f}".replace(".", ",")


def analyse(path):
    frame = cv2.imread(path)  # BGR, seperti frame dari av.VideoFrame.to_ndarray(format="bgr24")
    if frame is None:
        raise FileNotFoundError(path)
    h, w = frame.shape[:2]

    frame = enhance_low_light(frame)                 # CLAHE kanal Y (sama dengan process())
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)     # MediaPipe butuh RGB
    rgb.flags.writeable = False

    face_mesh = get_mediapipe_app()                  # instance baru per citra
    results = face_mesh.process(rgb)
    face_mesh.close()
    if not results.multi_face_landmarks:
        return dict(w=w, h=h, found=False, frame=frame)

    lm = results.multi_face_landmarks[0].landmark
    ear, (left_c, right_c) = calculate_avg_ear(lm, EYE_IDXS["left"], EYE_IDXS["right"], w, h)
    # EAR per mata (calculate_avg_ear hanya mengembalikan rata-rata)
    from deteksi_kantuk import get_ear
    ear_l, _ = get_ear(lm, EYE_IDXS["left"], w, h)
    ear_r, _ = get_ear(lm, EYE_IDXS["right"], w, h)
    mar, mouth_c = get_mar(lm, MOUTH_IDXS, w, h)
    angle, lpt, rpt = get_head_tilt_angle(lm, w, h)

    kondisi = []
    if mar > MAR_THRESH:
        kondisi.append("Menguap")
    if ear < EAR_THRESH:
        kondisi.append("Mata tertutup")
    if abs(angle) > TILT_THRESH:
        kondisi.append("Kepala miring")
    kesimpulan = (kondisi[0] + "".join(" dan " + k.lower() for k in kondisi[1:])) if kondisi else "Normal"

    return dict(w=w, h=h, found=True, frame=frame, ear=ear, ear_l=ear_l, ear_r=ear_r,
                mar=mar, angle=angle, left_c=left_c, right_c=right_c, mouth_c=mouth_c,
                lpt=lpt, rpt=rpt, kesimpulan=kesimpulan)


def draw(res):
    """Anotasi memakai fungsi gambar aplikasi; citra kecil diperbesar agar teks terbaca."""
    frame = res["frame"].copy()
    s = max(1.0, MIN_FIG_WIDTH / res["w"])
    if s > 1.0:
        frame = cv2.resize(frame, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    sc = lambda pts: [(int(round(x * s)), int(round(y * s))) for (x, y) in pts] if pts else pts
    H, W = frame.shape[:2]
    if not res["found"]:
        plot_text(frame, "WAJAH TIDAK TERDETEKSI", (10, H // 2), RED)
        return frame

    eye_color = RED if res["ear"] < EAR_THRESH else GREEN
    yawn = res["mar"] > MAR_THRESH
    tilt_color = YELLOW if abs(res["angle"]) > TILT_THRESH else GREEN
    plot_eye_landmarks(frame, sc(res["left_c"]), sc(res["right_c"]), eye_color)
    plot_mouth_landmarks(frame, sc(res["mouth_c"]), ORANGE)
    lpt = sc([res["lpt"]])[0] if res["lpt"] else None
    rpt = sc([res["rpt"]])[0] if res["rpt"] else None
    plot_head_tilt(frame, lpt, rpt, res["angle"], tilt_color)

    ui = max(0.45, min(1.25, W / 640))
    pad, line = int(10 * ui), int(30 * ui)
    plot_text(frame, f"EAR: {round(res['ear'], 3)}", (pad, line), eye_color)
    plot_text(frame, f"MAR: {round(res['mar'], 3)}", (pad, line * 2), ORANGE if yawn else GREEN)
    plot_text(frame, f"TILT: {res['angle']:.1f}deg", (pad, line * 3), tilt_color)
    if res["ear"] < EAR_THRESH:
        plot_text(frame, "MATA TERTUTUP!", (pad, int(H / 2 - line * 0.7)), RED)
    if yawn:
        plot_text(frame, "MENGUAP TERDETEKSI!", (pad, int(H / 2 + line * 0.7)), ORANGE)
    if abs(res["angle"]) > TILT_THRESH:
        plot_text(frame, "KEPALA MIRING!", (pad, int(H / 2 + line * 2)), YELLOW)
    return frame


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = []
    for name, ket in FILES:
        res = analyse(os.path.join(HERE, name))
        cv2.imwrite(os.path.join(OUT_DIR, "uji_" + os.path.splitext(name)[0] + ".jpg"), draw(res),
                    [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not res["found"]:
            rows.append([f"{name} ({ket})", f"{res['w']} x {res['h']}", "-", "-", "-", "-", "-",
                         "Wajah tidak terdeteksi"])
            continue
        rows.append([f"{name} ({ket})", f"{res['w']} x {res['h']}",
                     koma(res["ear_l"]), koma(res["ear_r"]), koma(res["ear"]), koma(res["mar"]),
                     koma(res["angle"], 1) + " derajat", res["kesimpulan"]])

    header = ["Citra", "Ukuran (piksel)", "EAR kiri", "EAR kanan", "EAR rata-rata", "MAR", "Sudut",
              "Kesimpulan sistem"]
    with open(os.path.join(OUT_DIR, "hasil_uji_statis.csv"), "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f, delimiter=";")
        wr.writerow(header)
        wr.writerows(rows)
    import mediapipe
    print("mediapipe", mediapipe.__version__, "| opencv", cv2.__version__, "| numpy", np.__version__)
    print(" | ".join(header))
    for r in rows:
        print(" | ".join(r))
    print("\nGambar & CSV tersimpan di:", OUT_DIR)


if __name__ == "__main__":
    main()
