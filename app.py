import os
import random
import time
from io import BytesIO
from datetime import date, timedelta

import pandas as pd
import qrcode
import streamlit as st
from twilio.rest import Client
from dotenv import load_dotenv

from storage_utils import (
    DEFAULT_FOLDERS,
    ensure_storage,
    load_entries,
    save_entries,
    load_petpooja_entries,
    save_petpooja_entries,
    update_vendor_memory,
    register_user,
    validate_login,
    reset_password,
    clean_phone,
    get_presigned_s3_url,
    append_petpooja_entry,
    append_entry,
    init_db,
    load_entries_for_user,
    update_entry_by_id,
    delete_entry_by_id,
    normalize_date_ddmmyyyy,
    delete_entries_by_ids,
    get_business_profile,
    upsert_business_profile,
    load_restaurant_uploaders,
    add_restaurant_uploader,
    deactivate_restaurant_uploader,
    load_financial_todos,
    add_financial_todo,
    update_financial_todo_status,
    delete_financial_todo,
    load_user_categories,
    add_user_category,
    delete_user_category,
    load_petpooja_entries_for_user,
    get_dashboard_totals_for_user,
    get_petpooja_total_for_user
)

load_dotenv()

st.set_page_config(
    page_title="FinWise Bills",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
[data-testid="stSidebarNav"] {
    display: none !important;
}
</style>
""", unsafe_allow_html=True)

@st.cache_resource
def initialize_database():
    init_db()
    return True

initialize_database()

@st.cache_data(ttl=300)
def cached_load_entries():
    return load_entries()

@st.cache_data(ttl=300)
def cached_load_entries_for_user(phone, limit=100):
    return load_entries_for_user(phone, limit)

@st.cache_data(ttl=300)
def cached_load_petpooja_entries_for_user(phone, limit=1000):
    return load_petpooja_entries_for_user(phone, limit)

@st.cache_data(ttl=300)
def cached_load_petpooja_entries():
    return load_petpooja_entries()

@st.cache_data(ttl=120)
def cached_dashboard_totals(phone, start_date=None, end_date=None):
    return get_dashboard_totals_for_user(phone, start_date, end_date)

@st.cache_data(ttl=120)
def cached_petpooja_total(phone, start_date=None, end_date=None):
    return get_petpooja_total_for_user(phone, start_date, end_date)

ensure_storage()



# -----------------------------
# Helpers
# -----------------------------
def read_petpooja_file(uploaded_file):
    file_name = uploaded_file.name.lower()
    try:
        if file_name.endswith(".xlsx"):
            return pd.read_excel(uploaded_file, engine="openpyxl", header=None)
        if file_name.endswith(".xls"):
            try:
                return pd.read_excel(uploaded_file, engine="xlrd", header=None)
            except Exception:
                uploaded_file.seek(0)
                return pd.read_html(uploaded_file)[0]
        uploaded_file.seek(0)
        return pd.read_html(uploaded_file)[0]
    except Exception as e:
        raise Exception(f"Could not read Petpooja file: {e}")


def build_petpooja_report_df(raw_df):
    header_row_index = None
    for i in range(len(raw_df)):
        row_values = raw_df.iloc[i].astype(str).str.strip().str.lower().tolist()
        if "date" in row_values and ("order no." in row_values or "order no" in row_values):
            header_row_index = i
            break

    if header_row_index is None:
        return pd.DataFrame()

    headers = raw_df.iloc[header_row_index].tolist()
    petpooja_df = raw_df.iloc[header_row_index + 1:].copy()
    petpooja_df.columns = headers
    petpooja_df = petpooja_df.dropna(how="all")

    if "Order No." in petpooja_df.columns:
        petpooja_df = petpooja_df[
            petpooja_df["Order No."].astype(str).str.lower().str.strip() != "total"
        ]

    if "Date" in petpooja_df.columns:
        petpooja_df["date_parsed"] = pd.to_datetime(
        petpooja_df["Date"],
        errors="coerce",
        dayfirst=True
)
    else:
        petpooja_df["date_parsed"] = pd.NaT

    amount_col = None
    for col in ["My Amount", "Total", "Total + Tip", "Net Amount", "Amount"]:
        if col in petpooja_df.columns:
            amount_col = col
            break

    petpooja_df["petpooja_total"] = (
        pd.to_numeric(petpooja_df[amount_col], errors="coerce").fillna(0)
        if amount_col
        else 0.0
    )

    payment_col = None
    for col in ["Payment Mode", "Payment Method", "Mode", "Payment Type"]:
        if col in petpooja_df.columns:
            payment_col = col
            break

    petpooja_df["payment_method"] = (
        petpooja_df[payment_col].astype(str).str.strip() if payment_col else "Unknown"
    )

    return petpooja_df

PETPOOJA_INPUT_COLUMNS = [
    "Order No.",
    "Date",
    "Payment Type",
    "Order Type",
    "Area Type",
    "My Amount",
    "Discount",
    "Delivery Charge",
    "container",
    "Water bottle",
    "Additional Charge",
    "Other Deduction Charge",
    "SGST (A)",
    "CGST (A)",
    "Waived Off",
    "Total",
    "Assign To",
    "Biller Name",
    "Reason",
    "Tip",
    "Total + Tip",
]

def normalize_saved_petpooja_df(df):
    if df.empty:
        return df

    if "Date" in df.columns:
        df["date_parsed"] = pd.to_datetime(
            df["Date"],
            errors="coerce",
            dayfirst=True
        )
    elif "date" in df.columns:
        df["date_parsed"] = pd.to_datetime(
            df["date"],
            errors="coerce",
            dayfirst=True
        )
    else:
        df["date_parsed"] = pd.NaT

    if "Total" in df.columns:
        df["petpooja_total"] = pd.to_numeric(df["Total"], errors="coerce").fillna(0)
    elif "total" in df.columns:
        df["petpooja_total"] = pd.to_numeric(df["total"], errors="coerce").fillna(0)
    else:
        df["petpooja_total"] = 0.0

    if "Payment Type" in df.columns:
        df["payment_method"] = df["Payment Type"].astype(str).str.strip()
    elif "payment_method" not in df.columns:
        df["payment_method"] = "Unknown"

    if "user_phone" not in df.columns:
        df["user_phone"] = ""

    return df


def make_petpooja_duplicate_key(row, phone):
    date = str(row.get("Date", row.get("date_parsed", ""))).strip()
    order_no = str(row.get("Order No.", row.get("Order No", ""))).strip()
    total = str(row.get("petpooja_total", "")).strip()
    payment = str(row.get("payment_method", "")).strip().lower()
    return f"{clean_phone(phone)}|{date}|{order_no}|{total}|{payment}"


def filter_date(df, date_col, date_filter, start_date, end_date):
    if df.empty or date_filter == "No Filter":
        return df
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    return df[(df[date_col].dt.date >= start_date) & (df[date_col].dt.date <= end_date)].copy()


def filter_by_phone(df, phone):
    if df.empty:
        return df
    if "user_phone" not in df.columns:
        df["user_phone"] = ""
    phone_clean = clean_phone(phone)
    df["user_phone_clean"] = df["user_phone"].astype(str).apply(clean_phone)
    return df[df["user_phone_clean"] == phone_clean].copy()

def get_category_options_for_user(phone):
    default_categories = list(DEFAULT_FOLDERS)

    try:
        user_cat_df = load_user_categories(phone)
        custom_categories = (
            user_cat_df["category_name"]
            .dropna()
            .astype(str)
            .str.strip()
            .tolist()
        )
    except Exception:
        custom_categories = []

    combined = default_categories + custom_categories

    # remove duplicates but keep order
    return list(dict.fromkeys([c for c in combined if c]))


def metric_card(label, value, icon, bg, color):
    st.markdown(
        f'''
        <div style="background:white; border:1px solid #E2E8F0; border-radius:18px; padding:22px; display:flex; gap:18px; align-items:center; box-shadow:0 8px 24px rgba(15,23,42,0.05);">
            <div style="background:{bg}; color:{color}; width:62px; height:62px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:30px; font-weight:900;">{icon}</div>
            <div>
                <div style="color:#64748B; font-size:14px; font-weight:700;">{label}</div>
                <div style="color:{color}; font-size:30px; font-weight:900;">₹{value:,.2f}</div>
            </div>
        </div>
        ''',
        unsafe_allow_html=True,
    )

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+15559208533")

def mobile_table_html(df):
    return df.to_html(index=False, escape=False)


def send_password_reset_code(phone):
    code = str(random.randint(100000, 999999))

    to_number = f"whatsapp:+{clean_phone(phone)}"

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    client.messages.create(
        from_=TWILIO_WHATSAPP_FROM,
        to=to_number,
        body=f"Your FinWise password reset code is: {code}. This code expires in 10 minutes."
    )

    st.session_state.reset_code = code
    st.session_state.reset_phone = clean_phone(phone)
    st.session_state.reset_code_expiry = time.time() + 600
    st.write("Sending OTP to:", to_number)
    st.write("Sending from:", TWILIO_WHATSAPP_FROM)

    return True


# -----------------------------
# CSS
# -----------------------------
st.markdown("""
<style>
.stApp { background:#F8FAFC !important; }
header { background:white !important; }
.block-container { padding-top:2rem !important; padding-left:2rem !important; padding-right:2rem !important; max-width:1400px !important; }
section[data-testid="stSidebar"] { background:#FFFFFF !important; border-right:1px solid #E5E7EB; }
h1,h2,h3,h4,p,label,span { color:#0F172A !important; }
.stButton button { border-radius:12px !important; background:#2563EB !important; color:white !important; font-weight:700 !important; border:none !important; }
div[data-testid="stFileUploader"] { background:#FFFFFF; border:1px dashed #CBD5E1; border-radius:16px; padding:12px; }         
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
@media (max-width: 768px) {

    .stApp {
        color: #0F172A !important;
    }

    /* Select box */
    div[data-baseweb="select"] > div,
    div[data-baseweb="select"] span {
        background-color: #FFFFFF !important;
        color: #0F172A !important;
    }

    /* File uploader */
    section[data-testid="stFileUploader"],
    section[data-testid="stFileUploader"] div,
    section[data-testid="stFileUploader"] label {
        background-color: #FFFFFF !important;
        color: #0F172A !important;
    }

    section[data-testid="stFileUploader"] {
        border-radius: 18px !important;
        border: 1px dashed #BFDBFE !important;
    }

    /* Dataframes and tables */
    div[data-testid="stDataFrame"],
    div[data-testid="stDataFrame"] div,
    div[data-testid="stDataFrame"] span,
    div[data-testid="stDataFrame"] p {
        background-color: #FFFFFF !important;
        color: #0F172A !important;
    }

    div[data-testid="stDataFrame"] {
        border-radius: 18px !important;
        border: 1px solid #BFDBFE !important;
        overflow: hidden !important;
    }

    /* Expander */
    details,
    details summary,
    details div {
        background-color: rgba(255,255,255,0.85) !important;
        color: #0F172A !important;
    }

    details {
        border-radius: 16px !important;
        border: 1px solid #BFDBFE !important;
    }

    /* Buttons */
    .stButton > button,
    div[data-testid="stButton"] > button,
    button[kind="primary"],
    button[kind="secondary"] {
        background: linear-gradient(90deg, #2563EB, #22C55E) !important;
        color: #0F172A !important;
        border: none !important;
        border-radius: 14px !important;
        font-weight: 700 !important;
    }

    .stButton > button p,
    div[data-testid="stButton"] > button p {
        color: #0F172A !important;
        font-weight: 700 !important;
    }

    /* Download button */
    div[data-testid="stDownloadButton"] button,
    div[data-testid="stDownloadButton"] button p {
        background: linear-gradient(90deg, #2563EB, #22C55E) !important;
        color: #0F172A !important;
        border-radius: 14px !important;
        font-weight: 700 !important;
    }
}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>

/* Main app background */
.stApp {
    background: linear-gradient(
        135deg,
        #F0F9FF 0%,
        #DBEAFE 45%,
        #DCFCE7 100%
    ) !important;
}

/* Main content area */
.main .block-container {
    background: rgba(255,255,255,0.72);
    padding: 2rem 2.5rem;
    border-radius: 24px;
    backdrop-filter: blur(10px);
    box-shadow: 0 12px 40px rgba(15,23,42,0.05);
}

/* Sidebar */
section[data-testid="stSidebar"] {
    background: linear-gradient(
        180deg,
        #EFF6FF 0%,
        #ECFDF5 100%
    ) !important;
    border-right: 1px solid #BFDBFE;
}

/* Titles */
h1, h2, h3 {
    color: #0F172A !important;
    font-weight: 850 !important;
    letter-spacing: -0.4px;
}

/* Metrics cards */
[data-testid="stMetric"] {
    background: rgba(255,255,255,0.78);
    border: 1px solid #BFDBFE;
    border-radius: 22px;
    padding: 20px;
    box-shadow: 0 10px 30px rgba(15,23,42,0.05);
}

/* Tables */
.stDataFrame {
    background: #FFFFFF !important;
    border-radius: 18px;
    border: 1px solid #BFDBFE;
    overflow: hidden;
}
            
/* Upload section */
[data-testid="stFileUploader"] {
    background: #FFFFFF !important;
    border: 1px solid #BFDBFE;
    border-radius: 18px;
    padding: 16px;
}

/* Selectbox */
.stSelectbox > div > div {
    background: #FFFFFF !important;
    color: #0F172A !important;
    border-radius: 14px;
    border: 1px solid #BFDBFE;
}

/* Buttons */
.stButton button {
    border-radius: 14px !important;
    border: none !important;
    background: linear-gradient(
        90deg,
        #2563EB,
        #22C55E
    ) !important;
    color: white !important;
    font-weight: 800 !important;
    box-shadow: 0 8px 24px rgba(37,99,235,0.18);
}

.streamlit-expanderHeader {
    background: #FFFFFF !important;
    color: #0F172A !important;
    border-radius: 14px !important;
    border: 1px solid #BFDBFE !important;
    font-weight: 700 !important;
}

/* Remove harsh white */
div[data-testid="stVerticalBlock"] > div {
    border-radius: 18px;
}

</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
#MainMenu {
    visibility: hidden;
}

footer {
    visibility: hidden;
}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
.stButton > button,
div[data-testid="stButton"] > button,
button[kind="primary"],
button[kind="secondary"] {
    background: linear-gradient(90deg, #2563EB, #22C55E) !important;
    color: #0F172A !important;
    border: none !important;
    border-radius: 14px !important;
    font-weight: 700 !important;
    box-shadow: 0 10px 24px rgba(37, 99, 235, 0.18) !important;
}

.stButton > button:hover,
div[data-testid="stButton"] > button:hover {
    background: linear-gradient(90deg, #1D4ED8, #16A34A) !important;
    color: white !important;
    border: none !important;
}

.stButton > button p,
div[data-testid="stButton"] > button p {
    color: inherit !important;
    font-weight: 700 !important;
}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>

/* Force Streamlit mobile to light mode without hiding dataframe text */
@media (max-width: 768px) {

    .stApp {
        background: linear-gradient(135deg, #F0F9FF 0%, #DBEAFE 45%, #DCFCE7 100%) !important;
        color: #0F172A !important;
    }

    /* Open WhatsApp link button */
    div[data-testid="stLinkButton"] a {
        background: linear-gradient(90deg, #2563EB, #22C55E) !important;
        color: #0F172A !important;
        border: none !important;
        border-radius: 14px !important;
        font-weight: 800 !important;
        text-decoration: none !important;
    }

    div[data-testid="stLinkButton"] a p {
        color: #0F172A !important;
        font-weight: 800 !important;
    }

    /* Normal buttons */
    .stButton > button,
    div[data-testid="stButton"] > button,
    div[data-testid="stDownloadButton"] button {
        background: linear-gradient(90deg, #2563EB, #22C55E) !important;
        color: #0F172A !important;
        border: none !important;
        border-radius: 14px !important;
        font-weight: 800 !important;
    }

    .stButton > button p,
    div[data-testid="stButton"] > button p,
    div[data-testid="stDownloadButton"] button p {
        color: #0F172A !important;
        font-weight: 800 !important;
    }

    /* File uploader */
    section[data-testid="stFileUploader"] {
        background: #FFFFFF !important;
        border: 1px dashed #BFDBFE !important;
        border-radius: 18px !important;
        padding: 16px !important;
    }

    section[data-testid="stFileUploader"] label,
    section[data-testid="stFileUploader"] p,
    section[data-testid="stFileUploader"] span {
        color: #0F172A !important;
    }

    /* Selectbox */
    div[data-baseweb="select"] > div {
        background: #FFFFFF !important;
        color: #0F172A !important;
        border-radius: 14px !important;
    }

    div[data-baseweb="select"] span {
        color: #0F172A !important;
    }

    /* Dataframe container only - do not style inner canvas */
    div[data-testid="stDataFrame"] {
        background: #FFFFFF !important;
        border: 1px solid #BFDBFE !important;
        border-radius: 18px !important;
        overflow: hidden !important;
    }

    /* Expander */
    details {
        background: #FFFFFF !important;
        border: 1px solid #BFDBFE !important;
        border-radius: 16px !important;
    }

    details summary,
    details summary p,
    details p,
    details span {
        color: #0F172A !important;
    }
}

</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
.mobile-table {
    background: white;
    border: 1px solid #BFDBFE;
    border-radius: 18px;
    overflow-x: auto;
    padding: 8px;
}

.mobile-table table {
    width: 100%;
    border-collapse: collapse;
    color: #0F172A !important;
    background: white !important;
}

.mobile-table th,
.mobile-table td {
    color: #0F172A !important;
    background: white !important;
    border-bottom: 1px solid #E5E7EB;
    padding: 10px;
    font-size: 14px;
    white-space: nowrap;
}

.mobile-table th {
    font-weight: 800;
}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
@media (max-width: 768px) {
    div[data-testid="stDataFrame"],
    div[data-testid="stDataEditor"] {
        display: none !important;
    }

    .mobile-table {
        display: block !important;
        background: white;
        border: 1px solid #BFDBFE;
        border-radius: 16px;
        overflow-x: auto;
        padding: 8px;
        margin-bottom: 16px;
    }

    .mobile-table table {
        min-width: 900px;
        width: 100%;
        border-collapse: collapse;
        background: white !important;
    }

    .mobile-table th,
    .mobile-table td {
        color: #0F172A !important;
        background: white !important;
        border-bottom: 1px solid #E5E7EB;
        padding: 10px;
        font-size: 13px;
        white-space: nowrap;
        text-align: left;
    }

    .mobile-table th {
        font-weight: 900;
    }
}

@media (min-width: 769px) {
    .mobile-table {
        display: none !important;
    }
}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
@media (max-width: 768px) {
    .st-key-desktop_expense_editor {
        display: none !important;
    }
}

@media (min-width: 769px) {
    .st-key-mobile_expense_editor {
        display: none !important;
    }
}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
@media (max-width: 768px) {
    .st-key-desktop_expense_editor,
    .st-key-desktop_expense_buttons {
        display: none !important;
    }
}

@media (min-width: 769px) {
    .st-key-mobile_expense_editor,
    .st-key-mobile_expense_buttons {
        display: none !important;
    }
}
</style>
""", unsafe_allow_html=True)
# -----------------------------
# Login state
# -----------------------------
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

if "user_phone" not in st.session_state:
    st.session_state.user_phone = ""

query_params = st.query_params

if query_params.get("logged_in") == "true" and query_params.get("phone"):
    st.session_state.logged_in = True
    st.session_state.user_phone = query_params.get("phone")

# -----------------------------
# Login page
# -----------------------------
if not st.session_state.logged_in:
    st.markdown("""
    <style>
    header {visibility:hidden;}
    .stApp {
    background: linear-gradient(
        135deg,
        #F0F9FF 0%,
        #DBEAFE 45%,
        #DCFCE7 100%
    ) !important;
    }
     
    .login-title {
    font-size:42px;
    font-weight:900;
    line-height:1.15;
    color:#0F172A !important;
    }
    .login-subtitle { color:#0F172A !important; font-size:17px; line-height:1.5; }
    label { color:white !important; font-weight:700 !important; }
    .stTextInput input { height:50px !important; border-radius:12px !important; text-align:center !important; }
    div[data-testid="stRadio"] { background:rgba(255,255,255,0.95); padding:8px; border-radius:16px; }
    .stRadio label { color:#0F172A !important; }
    .better-profit-text {
    color: #166534 !important;}
    </style>
    """, unsafe_allow_html=True)
    
    left, right = st.columns([1, 1], gap="large")
    with left:
        st.markdown(
            '<div style="background:linear-gradient(160deg,#2563EB,#38BDF8,#22C55E); padding:36px; border-radius:24px; color:white; min-height:560px;">'
            '<div style="font-size:34px; font-weight:800; margin-bottom:42px; color:white;">📊 FinWise</div>'
            '<div class="login-title">Smart bills.<br>Clear insights.<br><span style="display:inline-block; color:#166534 !important;">Better profits.</span></div>'
            '<div class="login-subtitle" style="margin-top:22px;">Upload bills on WhatsApp and let FinWise organize everything automatically.</div>'
            '<div style="background:rgba(255,255,255,0.16); border:1px solid rgba(255,255,255,0.22); border-radius:20px; padding:24px; margin-top:40px;">'

            '<div style="font-size:22px; font-weight:900; color:#0F172A; margin-bottom:20px;">Why use FinWise?</div>'

            '<div style="display:flex; align-items:center; gap:12px; margin-bottom:16px;">'
            '<div style="font-size:28px;"> ✅ </div>'
            '<div>'
            '<div style="font-size:16px; font-weight:850; color:#0F172A;">Send expense image on whatsapp</div>'
            '<div style="font-size:13px; color:#334155;">Bills and GPay screenshots on WhatsApp</div>'
            '</div>'
            '</div>'

            '<div style="display:flex; align-items:center; gap:12px; margin-bottom:16px;">'
            '<div style="font-size:28px;"> 📋 </div>'
            '<div>'
            '<div style="font-size:16px; font-weight:850; color:#0F172A;">Daily closing help</div>'
            '<div style="font-size:13px; color:#334155;">Track expenses for the day</div>'
            '</div>'
            '</div>'

            '<div style="display:flex; align-items:center; gap:12px; margin-bottom:16px;">'
            '<div style="font-size:28px;">📊</div>'
            '<div>'
            '<div style="font-size:16px; font-weight:850; color:#0F172A;"> Expense vs Income data with Petpooja Report </div>'
            '<div style="font-size:13px; color:#334155;">Income and payment summary</div>'
            '</div>'
            '</div>'

            '<div style="display:flex; align-items:center; gap:12px;">'
            '<div style="font-size:28px;">📁</div>'
            '<div>'
            '<div style="font-size:16px; font-weight:850; color:#0F172A;">Bill Images Saved in folders</div>'
            '<div style="font-size:13px; color:#334155;">Images organized by category/vendor</div>'
            '</div>'
            '</div>'

            '</div></div>',
            unsafe_allow_html=True,
        )

    with right:
        st.markdown("""
            <style>

            .login-title {
                color:white !important;
            }

            .login-subtitle {
                color:black !important;
            }

            </style>
            """, unsafe_allow_html=True)
        st.markdown(
            """
            <h1 class="login-title">
                Welcome Back!
            </h1>

            <p class="login-subtitle">
                Login to access your FinWise dashboard
            </p>
            """,
            unsafe_allow_html=True
        )
        auth_mode = st.radio("Choose option", ["Login", "Register", "Forgot Password"], horizontal=True, label_visibility="collapsed")
        phone_input = st.text_input("WhatsApp phone number", placeholder="+91 98765 43210")

        if auth_mode in ["Login", "Register"]:
            password_input = st.text_input("Password", type="password", placeholder="Enter password")

        if auth_mode == "Login":
            if st.button("Login", type="primary", use_container_width=True):
                if validate_login(phone_input, password_input):
                    st.session_state.logged_in = True
                    st.session_state.user_phone = clean_phone(phone_input)

                    st.query_params["logged_in"] = "true"
                    st.query_params["phone"] = clean_phone(phone_input)

                    st.rerun()
                else:
                    st.error("Invalid phone number or password.")
        elif auth_mode == "Register":
            if st.button("Create Account", type="primary", use_container_width=True):
                if not phone_input or not password_input:
                    st.error("Enter phone number and password.")
                else:
                    success, message = register_user(phone_input, password_input)
                    if success:
                        st.success(message)
                    else:
                        st.error(message)
        else:
            st.info("We will send a 6-digit reset code to your WhatsApp number.")

            if st.button("Send WhatsApp Code", type="primary", use_container_width=True):
                if not phone_input:
                    st.error("Enter your WhatsApp phone number.")
                else:
                    try:
                        send_password_reset_code(phone_input)
                        st.success("Reset code sent to your WhatsApp.")
                    except Exception as e:
                        st.error(f"Could not send WhatsApp code. Error: {str(e)}")

            reset_code_input = st.text_input(
                "Enter WhatsApp Code",
                placeholder="Enter 6-digit code"
            )

            new_password = st.text_input(
                "New Password",
                type="password",
                placeholder="Enter new password"
            )

            if st.button("Reset Password", use_container_width=True):
                if not phone_input or not reset_code_input or not new_password:
                    st.error("Enter phone number, WhatsApp code, and new password.")

                elif "reset_code" not in st.session_state:
                    st.error("Please request a WhatsApp code first.")

                elif time.time() > st.session_state.get("reset_code_expiry", 0):
                    st.error("Reset code expired. Please request a new code.")

                elif clean_phone(phone_input) != st.session_state.get("reset_phone", ""):
                    st.error("Phone number does not match the reset code.")

                elif reset_code_input.strip() != st.session_state.get("reset_code", ""):
                    st.error("Invalid WhatsApp code.")

                else:
                    success, message = reset_password(phone_input, new_password)

                    if success:
                        st.success("Password reset successfully. Please login.")

                        st.session_state.pop("reset_code", None)
                        st.session_state.pop("reset_phone", None)
                        st.session_state.pop("reset_code_expiry", None)
                    else:
                        st.error(message)

        st.markdown('<div style="text-align:center;color:black;font-weight:700;margin-top:26px;">🛡️ Your data is secure and organized privately.</div>', unsafe_allow_html=True)
        st.markdown("""
        <div style="text-align:center; margin-top:18px; font-size:14px;">
            <a href="/Privacy_Policy" target="_self" style="color:#2563EB; text-decoration:none; margin-right:18px;">
                Privacy Policy
            </a>
            <a href="/Terms_of_Service" target="_self" style="color:#2563EB; text-decoration:none;">
                Terms of Service
            </a>
        </div>
        """, unsafe_allow_html=True)
    st.stop()



# -----------------------------
# Header
# -----------------------------
whatsapp_number = "+15559208533"
whatsapp_link = f"https://wa.me/{whatsapp_number.replace('+', '')}"
phone = st.session_state.user_phone

with st.sidebar:
    st.markdown("### 📊 FinWise")
    st.markdown(f"**Logged in as:** {phone}")

    screen = st.radio(
        "Navigation",
        [
            "📊 Dashboard",
            "📁 Folder View",
            "⚙️ Settings",
            "🎓 Training",
        ],
        label_visibility="collapsed",
    )

    st.markdown("---")

    if st.button("🔄 Refresh Data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    if st.button("🚪 Logout", use_container_width=True):
        st.session_state.logged_in = False
        st.session_state.user_phone = ""
        st.query_params.clear()
        st.rerun()

    st.markdown("---")
    st.markdown("### 🎓 Quick Training")
    st.markdown("[▶️ Watch 3-Minute Setup Guide](#)")

top_left, top_right = st.columns([1.1, 1])
with top_left:
    st.markdown(
        '<div style="padding:10px 0 18px 0;">'
        '<h1 style="color:#0F172A; font-size:38px; margin:0; font-weight:850;">📊 FinWise Bills</h1>'
        '<p style="color:#475569; font-size:17px; margin-top:10px; line-height:1.5; max-width:520px;">WhatsApp bills, Petpooja reports, GPay screenshots and expenses in one dashboard.</p>'
        '</div>',
        unsafe_allow_html=True,
    )
with top_right:
    st.markdown(
        '<div style="background:white; border:1px solid #E2E8F0; border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(15,23,42,0.06);">'
        '<div style="display:flex; gap:16px; align-items:center;">'
        '<div style="background:#DCFCE7; color:#16A34A; width:54px; height:54px; border-radius:16px; display:flex; align-items:center; justify-content:center; font-size:30px; font-weight:800;">📞</div>'
        '<div style="flex:1;"><div style="font-size:18px; font-weight:800; color:#0F172A;">Send bills on WhatsApp</div>'
        '<div style="font-size:14px; color:#475569; line-height:1.4; margin-top:4px;">Send bill images, GPay screenshots, or expense messages to FinWise at +15559208533.</div></div>'
        '</div></div>',
        unsafe_allow_html=True,
    )
    st.link_button("🟢 Open WhatsApp", whatsapp_link, use_container_width=True)




# -----------------------------
# Screen 1
# -----------------------------
if screen == "📊 Dashboard":
    from datetime import date, timedelta

    date_filter = st.selectbox(
        "Select timeframe",
        ["No Filter", "Today", "Last 7 Days", "This Month", "Custom Range"],
        index=0,
        key="screen1_date_filter",
    )

    today = pd.Timestamp.today().date()
    start_date = today
    end_date = today

    if date_filter == "Today":
        start_date = today
        end_date = today

    elif date_filter == "Last 7 Days":
        start_date = today - pd.Timedelta(days=7)
        end_date = today

    elif date_filter == "This Month":
        start_date = today.replace(day=1)
        end_date = today

    elif date_filter == "Custom Range":
        default_end = date.today()
        default_start = default_end - timedelta(days=30)

        custom_dates = st.date_input(
            "Select custom range",
            value=(default_start, default_end),
            key="screen1_custom_date_range"
        )

        if isinstance(custom_dates, tuple) and len(custom_dates) == 2:
            start_date, end_date = custom_dates

        elif isinstance(custom_dates, tuple) and len(custom_dates) == 1:
            start_date = custom_dates[0]
            end_date = custom_dates[0]

        else:
            start_date = custom_dates
            end_date = custom_dates
    

    # WhatsApp entries
    if "expense_limit" not in st.session_state:
        st.session_state.expense_limit = 100

    df = cached_load_entries_for_user(phone, limit=st.session_state.expense_limit)
    if not df.empty and "is_deleted" in df.columns:
        df = df[df["is_deleted"].astype(str).str.lower() != "yes"].copy()

    if not df.empty:
        if "vendor" in df.columns:
            df = df[~df["vendor"].astype(str).str.lower().str.contains("petpooja", na=False)]
        if "description" in df.columns:
            df = df[~df["description"].astype(str).str.lower().str.contains("petpooja", na=False)]
        if "source" in df.columns:
            df = df[df["source"].astype(str).str.lower() != "petpooja"]

        df["date_parsed"] = pd.to_datetime(df.get("date", ""), errors="coerce")
        df["total"] = pd.to_numeric(df.get("total", 0), errors="coerce").fillna(0)
        df = filter_date(df, "date_parsed", date_filter, start_date, end_date)


    col_more, col_all = st.columns(2)

    with col_more:
        if st.button("Load 100 more bills", use_container_width=True):
            st.session_state.expense_limit += 100
            st.cache_data.clear()
            st.rerun()

    with col_all:
        if st.button("Show all bills", use_container_width=True):
            st.session_state.expense_limit = 5000
            st.cache_data.clear()
            st.rerun()
        

    # Saved Petpooja entries
    petpooja_saved_df = normalize_saved_petpooja_df(
    cached_load_petpooja_entries_for_user(phone, limit=1000))
    if not petpooja_saved_df.empty:
        petpooja_saved_df = petpooja_saved_df[
            petpooja_saved_df["user_phone"].astype(str).apply(clean_phone) == clean_phone(phone)
        ].copy()
        petpooja_saved_df = filter_date(petpooja_saved_df, "date_parsed", date_filter, start_date, end_date)
        petpooja_filtered_income = petpooja_saved_df["petpooja_total"].sum()
    else:
        petpooja_saved_df = pd.DataFrame()
        petpooja_filtered_income = 0.0

    # Totals
    if date_filter == "No Filter":
        sql_start_date = None
        sql_end_date = None
    else:
        sql_start_date = start_date
        sql_end_date = end_date

    whatsapp_income, total_expense = cached_dashboard_totals(
        phone,
        sql_start_date,
        sql_end_date
    )

    petpooja_filtered_income = cached_petpooja_total(
        phone,
        sql_start_date,
        sql_end_date
    )

    total_income = whatsapp_income + petpooja_filtered_income
    net_amount = total_income - total_expense

    m1, m2, m3 = st.columns(3)
    with m1:
        metric_card("Total Income", total_income, "↗", "#DCFCE7", "#16A34A")
    with m2:
        metric_card("Total Expenses", total_expense, "↘", "#FEE2E2", "#DC2626")
    with m3:
        metric_card("Net", net_amount, "💼", "#DBEAFE", "#2563EB")

    st.write("### ➕ Add Manual Expense")

    with st.expander("Add expense manually", expanded=False):
        c1, c2, c3 = st.columns(3)

        with c1:
            manual_date = st.date_input("Date", value=date.today(), key="manual_expense_date")
            manual_vendor = st.text_input("Vendor", placeholder="Example: Walmart", key="manual_expense_vendor")

        with c2:
            manual_category = st.selectbox(
                "Category",
                get_category_options_for_user(phone),
                key="manual_expense_category"
            )
            
            manual_amount = st.number_input(
                "Amount",
                min_value=0.0,
                step=1.0,
                format="%.2f",
                key="manual_expense_amount"
            )

        with c3:
            manual_type = st.selectbox("Type", ["expense", "income"], key="manual_expense_type")
            manual_description = st.text_input(
                "Description",
                placeholder="Example: milk purchase",
                key="manual_expense_description"
            )

        if st.button("➕ Add Expense", type="primary", use_container_width=True, key="add_manual_expense_btn"):
            if not manual_vendor.strip():
                st.error("Please enter vendor.")
            elif manual_amount <= 0:
                st.error("Please enter amount greater than 0.")
            else:
                manual_entry = {
                    "date": normalize_date_ddmmyyyy(str(manual_date)),
                    "transaction_type": manual_type,
                    "vendor": manual_vendor.strip().title(),
                    "user_phone": clean_phone(phone),
                    "uploaded_by": clean_phone(phone),
                    "description": manual_description.strip() or manual_vendor.strip(),
                    "category": manual_category,
                    "folder": manual_category,
                    "subtotal": float(manual_amount),
                    "tax": 0,
                    "total": float(manual_amount),
                    "currency": "INR",
                    "confidence": "manual",
                    "reason": "Manual dashboard entry",
                    "image_path": "",
                    "source": "Manual Dashboard",
                }

                append_entry(manual_entry)

                update_vendor_memory(
                    user_phone=phone,
                    vendor=manual_vendor.strip().title(),
                    category=manual_category,
                    folder=manual_category,
                )

                st.success("Manual expense added successfully.")
                st.cache_data.clear()
                st.rerun()

    st.write("### Expenses from WhatsApp")

    if df.empty:
        st.info("No WhatsApp expenses found for this phone number or selected timeframe.")
    else:
        original_df = df.reset_index(drop=True).copy()

        display_df = pd.DataFrame()
        display_df["db_id"] = original_df.get("id", "").astype(str)
        display_df["Expense Number"] = [
            f"EXP-{i:05d}" for i in range(1, len(original_df) + 1)
        ]

        display_df["Date"] = original_df.get("date", "")
        display_df["Type"] = original_df.get("transaction_type", "")
        display_df["Vendor"] = original_df.get("vendor", "")
        display_df["Description"] = original_df.get("description", "")
        display_df["Category"] = original_df.get("category", "")
        display_df["Amount"] = pd.to_numeric(
            original_df.get("total", 0),
            errors="coerce"
        ).fillna(0)
        display_df["Delete?"] = False

        category_options = get_category_options_for_user(phone)

        with st.container(key="desktop_expense_editor"):
            edited_df = st.data_editor(
                display_df,
                use_container_width=True,
                num_rows="fixed",
                hide_index=True,
                height=360,
                key="screen1_expense_editor",
                column_config={
                    "Category": st.column_config.SelectboxColumn(
                        "Category",
                        options=category_options,
                        required=True,
                    ),
                    "Amount": st.column_config.NumberColumn(
                        "Amount",
                        min_value=0.0,
                        step=1.0,
                        format="₹%.2f",
                    ),
                    "Delete?": st.column_config.CheckboxColumn(
                        "Delete?",
                        help="Select this to delete the expense",
                        default=False,
                    ),
                },
                disabled=["Expense Number", "Type", "Description"],
            )

        with st.container(key="mobile_expense_editor"):
            st.info("For full table view, please log in from desktop.")
            st.markdown("#### Mobile Edit View")

            mobile_rows = []

            mobile_display_df = display_df.head(25)

            for i, row in mobile_display_df.iterrows():
                with st.expander(
                    f'{row["Expense Number"]} • {row["Date"]} • {row["Vendor"]} • ₹{float(row["Amount"]):,.2f}',
                    expanded=False
                ):
                    st.write(f'**Date:** {row["Date"]}')
                    st.write(f'**Type:** {row["Type"]}')
                    st.write(f'**Vendor:** {row["Vendor"]}')
                    st.write(f'**Description:** {row["Description"]}')

                    current_category = str(row.get("Category", "Uncategorized"))
                    category_index = (
                        category_options.index(current_category)
                        if current_category in category_options
                        else category_options.index("Uncategorized")
                    )

                    new_category = st.selectbox(
                        "Category",
                        category_options,
                        index=category_index,
                        key=f"mobile_category_{i}"
                    )

                    new_amount = st.number_input(
                        "Amount",
                        min_value=0.0,
                        value=float(row["Amount"]),
                        step=1.0,
                        key=f"mobile_amount_{i}"
                    )

                    delete_row = st.checkbox(
                        "Delete this expense",
                        value=False,
                        key=f"mobile_delete_{i}"
                    )

                    mobile_rows.append({
                        **row.to_dict(),
                        "Category": new_category,
                        "Amount": new_amount,
                        "Delete?": delete_row,
                    })

            edited_df_mobile = pd.DataFrame(mobile_rows)

        def save_edited_expenses(edited_data, original_df, phone):
            updated_count = 0

            for _, row in edited_data.iterrows():
                entry_id = str(row.get("db_id", "")).strip()

                if not entry_id:
                    continue

                original_match = original_df[original_df["id"].astype(str) == entry_id]

                if original_match.empty:
                    continue

                original_row = original_match.iloc[0]

                new_category = str(row.get("Category", "")).strip()
                new_vendor = str(row.get("Vendor", "")).strip()
                new_amount = float(pd.to_numeric(row.get("Amount", 0), errors="coerce") or 0)

                old_category = str(original_row.get("category", "")).strip()
                old_vendor = str(original_row.get("vendor", "")).strip()
                old_amount = float(pd.to_numeric(original_row.get("total", 0), errors="coerce") or 0)

                if (
                    new_category == old_category
                    and new_vendor == old_vendor
                    and new_amount == old_amount
                ):
                    continue

                update_entry_by_id(
                    entry_id=entry_id,
                    category=new_category,
                    amount=new_amount,
                    vendor=new_vendor,
                )

                updated_count += 1

                if new_vendor and new_category:
                    update_vendor_memory(
                        user_phone=phone,
                        vendor=new_vendor,
                        category=new_category,
                        folder=new_category,
                    )

            return updated_count


        def delete_selected_expenses(edited_data, original_df, phone):
            selected_rows = edited_data[edited_data["Delete?"] == True]

            if selected_rows.empty:
                return 0

            ids_to_delete = selected_rows["db_id"].astype(str).str.strip().tolist()

            return delete_entries_by_ids(ids_to_delete)
        
        with st.container(key="desktop_expense_buttons"):
            if st.button("💾 Save Changes", type="primary", use_container_width=True, key="desktop_save_expenses"):
                save_edited_expenses(edited_df, original_df, phone)
                st.success("Desktop changes saved.")
                st.cache_data.clear()
                st.rerun()

            if st.button("🗑️ Delete Selected Expenses", use_container_width=True, key="desktop_delete_expenses"):
                deleted_count = delete_selected_expenses(edited_df, original_df, phone)

                if deleted_count == 0:
                    st.warning("Please select at least one expense to delete.")
                else:
                    st.success(f"Deleted {deleted_count} expense(s).")
                    st.cache_data.clear()
                    st.rerun()




        with st.container(key="mobile_expense_buttons"):
            if st.button("💾 Save Mobile Changes", type="primary", use_container_width=True, key="mobile_save_expenses"):
                save_edited_expenses(edited_df_mobile, original_df, phone)
                st.success("Mobile changes saved.")
                st.cache_data.clear()
                st.rerun()

            if st.button("🗑️ Delete Selected Mobile Expenses", use_container_width=True, key="mobile_delete_expenses"):
                deleted_count = delete_selected_expenses(edited_df_mobile, original_df, phone)

                if deleted_count == 0:
                    st.warning("Please select at least one expense to delete.")
                else:
                    st.success(f"Deleted {deleted_count} expense(s).")
                    st.cache_data.clear()
                    st.rerun()

    st.write("### Petpooja Sales Summary")
    st.metric("Petpooja Total Sales", f"₹{petpooja_filtered_income:,.2f}")

    # Upload Petpooja first, then reload saved file
    st.write("### Upload Petpooja Daily/Monthly Sales Summary")
    uploaded_sales_file = st.file_uploader("Upload Petpooja Daily/Monthly Sales Summary", type=["xls", "xlsx", "html"])

    if uploaded_sales_file is not None:
        try:
            raw_sales_df = read_petpooja_file(uploaded_sales_file)
            petpooja_report_df = build_petpooja_report_df(raw_sales_df)

            if petpooja_report_df.empty:
                st.warning("Could not find Petpooja order rows in this report.")
            else:
                petpooja_report_df["user_phone"] = clean_phone(phone)
                petpooja_report_df["duplicate_key"] = petpooja_report_df.apply(
                    lambda row: make_petpooja_duplicate_key(row, phone), axis=1
                )

                existing_petpooja_df = normalize_saved_petpooja_df(load_petpooja_entries())
                existing_keys = set(existing_petpooja_df["duplicate_key"].astype(str)) if not existing_petpooja_df.empty and "duplicate_key" in existing_petpooja_df.columns else set()

                new_petpooja_df = petpooja_report_df[
                    ~petpooja_report_df["duplicate_key"].astype(str).isin(existing_keys)
                ].copy()
                duplicate_count = len(petpooja_report_df) - len(new_petpooja_df)

                if not new_petpooja_df.empty:
                    for _, row in new_petpooja_df.iterrows():
                        append_petpooja_entry(row.to_dict())

                st.success(f"Petpooja processed. Added {len(new_petpooja_df)} new records. Skipped {duplicate_count} duplicates.")
        except Exception as e:
            st.error(f"Could not read Petpooja file: {e}")

    if not petpooja_saved_df.empty:
        payment_summary = (
            petpooja_saved_df.groupby("payment_method")["petpooja_total"]
            .sum()
            .reset_index()
            .sort_values("petpooja_total", ascending=False)
        )
        st.write("### Petpooja Payment Summary")
        payment_summary = payment_summary.reset_index(drop=True)

        st.markdown(
    f'<div class="mobile-table">{mobile_table_html(payment_summary)}</div>',
    unsafe_allow_html=True
)

        with st.expander("Preview Petpooja Report"):
            petpooja_preview_df = petpooja_saved_df.reset_index(drop=True).copy()

            if "Date" in petpooja_preview_df.columns:
                petpooja_preview_df["Date"] = pd.to_datetime(
                    petpooja_preview_df["Date"],
                    errors="coerce",
                    dayfirst=True
                ).dt.strftime("%d-%b-%Y")

            available_petpooja_columns = [
                col for col in PETPOOJA_INPUT_COLUMNS
                if col in petpooja_preview_df.columns
            ]

            st.markdown(
                f'<div class="mobile-table">{mobile_table_html(petpooja_preview_df[available_petpooja_columns])}</div>',
                unsafe_allow_html=True
            )

    st.write("### Download Report")

if st.button("📄 Prepare Excel Download", use_container_width=True):
    output = BytesIO()

    export_df = df.drop(
        columns=[
            "user_phone_clean", "id", "date_parsed", "transaction_type",
            "user_phone", "category", "subtotal", "tax", "currency",
            "confidence", "reason", "image_path", "created_at",
            "payment_method", "source", "duplicate_key"
        ],
        errors="ignore"
    )

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        export_df = export_df.reset_index(drop=True)

        export_df.insert(
            0,
            "Expense Number",
            [f"EXP-{i:05d}" for i in range(1, len(export_df) + 1)]
        )

        export_df.to_excel(writer, index=False, sheet_name="WhatsApp Expenses")

        if not petpooja_saved_df.empty:
            petpooja_saved_df.drop(
                columns=["date_parsed"],
                errors="ignore"
            ).to_excel(writer, index=False, sheet_name="Petpooja Sales")

        pd.DataFrame([
            {"Metric": "Total Income", "Amount": total_income},
            {"Metric": "Total Expense", "Amount": total_expense},
            {"Metric": "Net", "Amount": net_amount},
        ]).to_excel(writer, index=False, sheet_name="Totals")

    output.seek(0)

    st.download_button(
        "⬇️ Download Excel",
        data=output,
        file_name="finwise_extracted_bills.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True
    )

elif screen == "📁 Folder View":

    st.subheader("📁 File Explorer")

    
    df = cached_load_entries_for_user(phone, limit=1000)

    if not df.empty and "is_deleted" in df.columns:
        df = df[df["is_deleted"].astype(str).str.lower() != "yes"].copy()

    if df.empty:
        st.info("No bills found for this phone number.")
        st.stop()

    df["total"] = pd.to_numeric(df.get("total", 0), errors="coerce").fillna(0)

    st.caption("Open a category folder, then open a vendor folder to view bills.")

    for folder in DEFAULT_FOLDERS:

        folder_df = df[df["folder"] == folder].copy() if "folder" in df.columns else pd.DataFrame()

        if folder_df.empty:
            continue

        folder_total = folder_df["total"].sum()
        folder_count = len(folder_df)

        with st.expander(f"📁 {folder}  •  {folder_count} bills  •  ₹{folder_total:,.2f}"):

            vendors = sorted(
                folder_df["vendor"].fillna("Unknown Vendor").astype(str).unique()
            )

            for vendor in vendors:

                vendor_df = folder_df[
                    folder_df["vendor"].fillna("Unknown Vendor").astype(str) == vendor
                ].copy()

                vendor_total = vendor_df["total"].sum()
                vendor_count = len(vendor_df)

                with st.expander(f"🏪 {vendor}  •  {vendor_count} bills  •  ₹{vendor_total:,.2f}"):

                    for _, row in vendor_df.iterrows():

                        bill_date = row.get("date", "")
                        description = row.get("description", "")
                        total = row.get("total", 0)
                        image_path = row.get("image_path", "")

                        st.markdown(
                            f"""
                            <div style="
                                background:white;
                                border:1px solid #E2E8F0;
                                border-radius:14px;
                                padding:16px;
                                margin-bottom:12px;
                            ">
                                <div style="font-weight:800; font-size:16px;">
                                    🧾 {description if description else "Bill"}
                                </div>
                                <div style="color:#64748B; margin-top:6px;">
                                    Date: {bill_date} &nbsp; | &nbsp; Amount: ₹{total:,.2f}
                                </div>
                            </div>
                            """,
                            unsafe_allow_html=True
                        )

                        if image_path:
                            with st.expander("View bill image"):
                                display_image_path = get_presigned_s3_url(image_path)
                                st.image(display_image_path, width=450)
                        else:
                            st.caption("No image available.")

                        st.divider()
elif screen == "⚙️ Settings":
    st.subheader("⚙️ Settings")
    st.caption("Manage your business profile, staff uploaders, and financial to-dos.")

    tab1, tab2, tab3, tab4 = st.tabs([
    "🏪 Business Profile",
    "👥 WhatsApp Uploaders",
    "✅ Financial To-Dos",
    "🏷️ Categories",
])

    # -----------------------------
    # Business Profile
    # -----------------------------
    with tab1:
        st.markdown("### 🏪 Business Profile")
        st.caption("This information will be used later to personalize summaries, reports, and business insights.")

        profile = get_business_profile(phone)

        c1, c2 = st.columns(2)

        with c1:
            business_name = st.text_input(
                "Business Name",
                value=profile.get("business_name", ""),
                placeholder="Example: FinWise Restaurant",
                key="settings_business_name",
            )

            owner_name = st.text_input(
                "Owner Name",
                value=profile.get("owner_name", ""),
                placeholder="Example: Aiswarya",
                key="settings_owner_name",
            )

            business_type = st.selectbox(
                "Business Type",
                ["Restaurant", "Cafe", "Cloud Kitchen", "Grocery Store", "Retail", "Service Business", "Other"],
                index=0 if not profile.get("business_type") else
                ["Restaurant", "Cafe", "Cloud Kitchen", "Grocery Store", "Retail", "Service Business", "Other"].index(profile.get("business_type"))
                if profile.get("business_type") in ["Restaurant", "Cafe", "Cloud Kitchen", "Grocery Store", "Retail", "Service Business", "Other"]
                else 0,
                key="settings_business_type",
            )

        with c2:
            business_email = st.text_input(
                "Business Email",
                value=profile.get("business_email", ""),
                placeholder="Example: owner@business.com",
                key="settings_business_email",
            )

            currency = st.selectbox(
                "Currency",
                ["INR", "CAD", "USD"],
                index=["INR", "CAD", "USD"].index(profile.get("currency", "INR"))
                if profile.get("currency", "INR") in ["INR", "CAD", "USD"]
                else 0,
                key="settings_currency",
            )

            timezone = st.selectbox(
                "Timezone",
                ["Asia/Kolkata", "America/Toronto", "America/New_York", "UTC"],
                index=["Asia/Kolkata", "America/Toronto", "America/New_York", "UTC"].index(profile.get("timezone", "Asia/Kolkata"))
                if profile.get("timezone", "Asia/Kolkata") in ["Asia/Kolkata", "America/Toronto", "America/New_York", "UTC"]
                else 0,
                key="settings_timezone",
            )

        st.write("Account phone:", clean_phone(phone))

        if st.button("💾 Save Business Profile", type="primary", use_container_width=True):
            upsert_business_profile(
                owner_phone=phone,
                business_name=business_name.strip(),
                owner_name=owner_name.strip(),
                business_type=business_type,
                business_email=business_email.strip(),
                currency=currency,
                timezone=timezone,
            )

            st.success("Business profile saved.")
            st.cache_data.clear()
            st.rerun()

    # -----------------------------
    # WhatsApp Uploaders
    # -----------------------------
    with tab2:
        st.markdown("### 👥 WhatsApp Uploaders")
        st.caption("Add staff numbers here. Staff can upload bills through WhatsApp, but bills will appear in the owner's dashboard.")

        with st.expander("➕ Add New Uploader", expanded=False):
            uc1, uc2 = st.columns(2)

            with uc1:
                new_uploader_name = st.text_input(
                    "Uploader Name",
                    placeholder="Example: Kitchen Staff",
                    key="new_uploader_name",
                )

            with uc2:
                new_uploader_phone = st.text_input(
                    "Uploader WhatsApp Number",
                    placeholder="Example: +91 98765 43210",
                    key="new_uploader_phone",
                )

            if st.button("Add Uploader", type="primary", use_container_width=True):
                if not new_uploader_name.strip():
                    st.error("Enter uploader name.")
                elif not clean_phone(new_uploader_phone):
                    st.error("Enter valid uploader phone.")
                else:
                    add_restaurant_uploader(
                        owner_phone=phone,
                        uploader_phone=new_uploader_phone,
                        uploader_name=new_uploader_name,
                    )

                    st.success("Uploader added.")
                    st.cache_data.clear()
                    st.rerun()

        uploaders_df = load_restaurant_uploaders()

        if not uploaders_df.empty:
            uploaders_df = uploaders_df[
                uploaders_df["owner_phone"].astype(str).apply(clean_phone) == clean_phone(phone)
            ].copy()

        if uploaders_df.empty:
            st.info("No staff uploaders added yet.")
        else:
            st.markdown("#### Current Uploaders")

            for _, row in uploaders_df.iterrows():
                uploader_id = row.get("id", "")
                uploader_name = row.get("uploader_name", "")
                uploader_phone = row.get("uploader_phone", "")
                active = str(row.get("active", "")).lower().strip()

                status_label = "Active" if active in ["yes", "true", "1", "active"] else "Inactive"

                col1, col2, col3 = st.columns([3, 3, 2])

                with col1:
                    st.write(f"**{uploader_name}**")

                with col2:
                    st.write(f"`{uploader_phone}`")

                with col3:
                    if status_label == "Active":
                        if st.button("Disable", key=f"disable_uploader_{uploader_id}"):
                            deactivate_restaurant_uploader(uploader_id)
                            st.success("Uploader disabled.")
                            st.cache_data.clear()
                            st.rerun()
                    else:
                        st.caption("Inactive")

                st.divider()

    # -----------------------------
    # Financial To-Dos
    # -----------------------------
    with tab3:
        st.markdown("### ✅ Financial To-Dos")
        st.caption("Track supplier payments, taxes, license renewals, collections, and finance follow-ups.")

        with st.expander("➕ Add Financial To-Do", expanded=False):
            tc1, tc2 = st.columns(2)

            with tc1:
                todo_title = st.text_input(
                    "Task",
                    placeholder="Example: Pay milk supplier",
                    key="todo_title",
                )

                todo_type = st.selectbox(
                    "Type",
                    ["Supplier Payment", "Tax", "License Renewal", "Rent", "Customer Collection", "Banking", "Other"],
                    key="todo_type",
                )

                todo_due_date = st.date_input(
                    "Due Date",
                    value=date.today(),
                    key="todo_due_date",
                )

            with tc2:
                todo_amount = st.number_input(
                    "Amount",
                    min_value=0.0,
                    step=100.0,
                    format="%.2f",
                    key="todo_amount",
                )

                todo_notes = st.text_area(
                    "Notes",
                    placeholder="Example: Confirm payment after UPI transfer",
                    key="todo_notes",
                )

            if st.button("Add To-Do", type="primary", use_container_width=True):
                if not todo_title.strip():
                    st.error("Enter a task.")
                else:
                    add_financial_todo(
                        owner_phone=phone,
                        title=todo_title,
                        todo_type=todo_type,
                        due_date=normalize_date_ddmmyyyy(str(todo_due_date)),
                        amount=todo_amount,
                        notes=todo_notes,
                    )

                    st.success("Financial to-do added.")
                    st.cache_data.clear()
                    st.rerun()

        todos_df = load_financial_todos(phone)

        if todos_df.empty:
            st.info("No financial to-dos added yet.")
        else:
            st.markdown("#### Open To-Dos")

            open_todos = todos_df[
                todos_df["status"].astype(str).str.lower().str.strip() != "done"
            ].copy()

            if open_todos.empty:
                st.success("No open financial to-dos.")
            else:
                for _, row in open_todos.iterrows():
                    todo_id = row.get("id", "")
                    title = row.get("title", "")
                    todo_type = row.get("todo_type", "")
                    due_date = row.get("due_date", "")
                    amount = float(pd.to_numeric(row.get("amount", 0), errors="coerce") or 0)
                    notes = row.get("notes", "")

                    with st.container():
                        c1, c2, c3 = st.columns([4, 2, 2])

                        with c1:
                            st.write(f"**{title}**")
                            st.caption(f"{todo_type} | Due: {due_date}")
                            if notes:
                                st.caption(notes)

                        with c2:
                            st.write(f"₹{amount:,.2f}")

                        with c3:
                            if st.button("Mark Done", key=f"done_todo_{todo_id}"):
                                update_financial_todo_status(todo_id, "done")
                                st.success("To-do marked done.")
                                st.cache_data.clear()
                                st.rerun()

                            if st.button("Delete", key=f"delete_todo_{todo_id}"):
                                delete_financial_todo(todo_id)
                                st.success("To-do deleted.")
                                st.cache_data.clear()
                                st.rerun()

                        st.divider()

            with st.expander("View Completed To-Dos"):
                done_todos = todos_df[
                    todos_df["status"].astype(str).str.lower().str.strip() == "done"
                ].copy()

                if done_todos.empty:
                    st.caption("No completed to-dos yet.")
                else:
                    st.dataframe(
                        done_todos[["title", "todo_type", "due_date", "amount", "notes", "status"]],
                        use_container_width=True,
                        hide_index=True,
                    )

    with tab4:
        st.markdown("### 🏷️ Categories")
        st.caption("Manage custom categories used for manual expenses, WhatsApp bills, and vendor rules.")

        with st.expander("➕ Add Category", expanded=False):
            new_category = st.text_input(
                "Category Name",
                placeholder="Example: Chicken, Milk, Rice",
                key="new_custom_category",
            )

            if st.button("Add Category", type="primary", use_container_width=True):
                if not new_category.strip():
                    st.error("Enter category name.")
                else:
                    added = add_user_category(phone, new_category)

                    if added:
                        st.success("Category added.")
                    else:
                        st.warning("Category already exists.")

                    st.cache_data.clear()
                    st.rerun()

        categories_df = load_user_categories(phone)

        if categories_df.empty:
            st.info("No custom categories added yet.")
        else:
            st.markdown("#### Current Categories")

            for _, row in categories_df.iterrows():
                category_id = row.get("id", "")
                category_name = row.get("category_name", "")

                col1, col2 = st.columns([4, 1])

                with col1:
                    st.write(f"**{category_name}**")

                with col2:
                    if st.button("Delete", key=f"delete_category_{category_id}"):
                        delete_user_category(category_id)
                        st.success("Category deleted.")
                        st.cache_data.clear()
                        st.rerun()

                st.divider()

elif screen == "🎓 Training":
    st.subheader("🎓 Training")

    st.markdown("""
    ### How to use FinWise

    **1. Send bill images on WhatsApp**  
    Take a clear photo of the bill and send it to FinWise.

    **2. Confirm category for new vendors**  
    If FinWise sees a new vendor, reply with category like Grocery, Meals, Utilities, etc.

    **3. Upload Petpooja report**  
    Use the upload section in Dashboard to add daily or monthly sales reports.

    **4. Review dashboard**  
    Check income, expenses, net amount, and download Excel.

    **Training video coming soon.**
    """)
# -----------------------------
# Footer
# -----------------------------
st.divider()
st.markdown(
    '<div style="background:#F8FAFC; border:1px solid #E5E7EB; padding:18px; border-radius:14px; margin-top:30px;">'
    '<h4 style="margin-bottom:8px;">Support & Feedback</h4>'
    '<p style="margin-bottom:6px;">Need help or want to share feedback?</p>'
    '<p style="margin-bottom:6px;"><strong>Email:</strong> aims.weautomate@gmail.com</p>'
    '<p style="margin-bottom:0;"><strong>WhatsApp:</strong> +14373241463</p>'
    '</div>',
    unsafe_allow_html=True,
)
st.markdown("""
<div style="text-align:center; margin-top:30px; font-size:14px; color:#475569;">
    <a href="/Privacy_Policy" target="_self" style="color:#2563EB; text-decoration:none; margin-right:18px;">
        Privacy Policy
    </a>
    <a href="/Terms_of_Service" target="_self" style="color:#2563EB; text-decoration:none;">
        Terms of Service
    </a>
</div>
""", unsafe_allow_html=True)
