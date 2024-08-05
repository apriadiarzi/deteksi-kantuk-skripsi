import streamlit as st
from ads import css_string

st.set_page_config(
    page_title="Pengenalan Sistem Pendeteksi Kantuk",
    page_icon="https://cdn-icons-png.flaticon.com/512/1464/1464723.png",
    layout="wide",  # centered, wide
    initial_sidebar_state="expanded",
    # menu_items={
    #     "About": "### Visit www.learnopencv.com for more exciting tutorials!!!",
    # },
)

st.title("Pengenalan")

st.markdown(css_string, unsafe_allow_html=True)