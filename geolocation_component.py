import os

import streamlit.components.v1 as components

# Komponen pelacak lokasi untuk email darurat — isinya di components/geolocation.
#
# Sengaja dideklarasikan di modul terpisah ini, bukan di file halaman utama.
# Streamlit menamai komponen dari nama modul pemanggilnya, dan untuk file
# halaman itu artinya nama file-nya: "2_🥱_Deteksi_Kantuk.geolocation". Emoji
# itu ikut masuk ke URL komponen (/component/2_%F0%9F%A5%B1_.../index.html),
# dan di Streamlit Cloud komponennya gagal dimuat ("Your app is having trouble
# loading the ... component"), padahal di lokal jalan normal. Dari modul ini
# namanya jadi "geolocation_component.geolocation" — URL-nya polos ASCII.
geolocation_component = components.declare_component(
    "geolocation",
    path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "components", "geolocation"),
)
