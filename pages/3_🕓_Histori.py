import streamlit as st
from database import get_history_by_username, get_user_id_by_username, get_history_detail
import os
import pickle
import time
from urllib.parse import unquote


def get_cookie():
    if os.path.exists("cookies.pkl"):
        cookies = pickle.load(open("cookies.pkl", "rb"))
        if cookies["expiry"] > time.time():
            return cookies["username"]
    return None

# Cek parameter URL
query_params = st.experimental_get_query_params()
param = query_params.get("waktu_mengantuk", [None])[0]

# Cek apakah user sudah login
user = get_cookie()

# Fungsi untuk menampilkan history
def display_history(username):
    history_data = get_history_by_username(username)
    
    if not history_data:
        st.markdown(f"<h5>Hallo {user}! <br/> maaf belum ada histori berkendara nih.</h5>", unsafe_allow_html=True)
        # if st.button("Tambah parameter"):
        #     st.experimental_set_query_params(waktu_mengantuk="test")
        #     st.experimental_rerun()
        # return
    else:
        st.markdown(f"<h5>Hallo {user}! <br/> berikut histori berkendara kamu:</h5>", unsafe_allow_html=True)
    
    for record in history_data:
        history_block = f"""
        <a href="?waktu_mengantuk={record[3]}" target="_self" style="text-decoration: none; color: inherit;">
            <div style="border: 1px solid #ff000047; padding: 10px; margin: 10px 0px; border-radius: 5px;">
                <p><strong>Start Time:</strong> {record[2]}</p>
                <p><strong>Stop Time:</strong> {record[3]}</p>
                <p><strong>Drowsiness Type:</strong> {record[4]}</p>
                <p><strong>Drowsiness Time:</strong> {record[5]}</p>
                <p><strong>Drowsiness Duration:</strong> {record[6]}</p>
            </div>
        </a>
        """
        st.markdown(history_block, unsafe_allow_html=True)

# Fungsi untuk menampilkan riwayat mengantuk
def display_detail_history(username):
    history_data = get_history_detail(username, unquote(param))
    
    if st.button("Kembali"):
            st.experimental_set_query_params()
            st.experimental_rerun()
            
    if not history_data:
        st.markdown(f"<h5>Hallo {user}! <br/> maaf histori berkendara yang kamu maksud belum ada.</h5>", unsafe_allow_html=True)
        # if st.button("Kembali"):
        #     st.experimental_set_query_params()
        #     st.experimental_rerun()
        return
    
    for record in history_data:
        history_block = f"""
        <div style="border: 1px solid #ff000047; padding: 10px; margin: 10px 0px; border-radius: 5px;">
            <p><strong>Start Time:</strong> {record[2]}</p>
            <p><strong>Stop Time:</strong> {record[3]}</p>
            <p><strong>Drowsiness Type:</strong> {record[4]}</p>
            <p><strong>Drowsiness Time:</strong> {record[5]}</p>
            <p><strong>Drowsiness Duration:</strong> {record[6]}</p>
        </div>
        """
        st.markdown(history_block, unsafe_allow_html=True)

# Tampilkan data jika username ditemukan
if param:
    # st.markdown('''<h5>Hallo {user}! <br/> berikut histori berkendara kamu:</h5>''', unsafe_allow_html=True)
    display_detail_history(user)
elif user:
    # st.markdown('''<h5>Hallo Apriadi! <br/> berikut histori berkendara kamu:</h5>''', unsafe_allow_html=True)
    display_history(user)
else:
    st.warning("No username cookie found.")