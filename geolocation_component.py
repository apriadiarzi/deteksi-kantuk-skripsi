import os

import streamlit.components.v1 as components

geolocation_component = components.declare_component(
    "geolocation",
    path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "components", "geolocation"),
)
