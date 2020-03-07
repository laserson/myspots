import streamlit as st
import pandas as pd
from airtable import Airtable

from myspots.utils import get_config, get_airtable


@st.cache
def load_config():
    config = get_config()
    return config


@st.cache
def load_places_from_airtable():
    config = load_config()
    airtable = get_airtable(config)
    record_list = airtable.get_all()
    df = pd.DataFrame([record["fields"] for record in record_list])
    return df


places_df = load_places_from_airtable()

st.map(places_df)
