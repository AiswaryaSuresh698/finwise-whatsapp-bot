import os
import re
import uuid
import json
import hashlib
from datetime import datetime

import pandas as pd
import gspread
from google.oauth2.service_account import Credentials


BASE_DIR = "finwise_storage"
CSV_PATH = os.path.join(BASE_DIR, "extracted_bills.csv")

DEFAULT_FOLDERS = [
    "Grocery",
    "Gas",
    "Internet",
    "Utilities",
    "Meals",
    "Rent",
    "Software",
    "Office Supplies",
    "Vehicle",
    "Professional Fees",
    "Insurance",
    "Travel",
    "Income",
    "Uncategorized",
]


# -----------------------------
# Google Sheets shared storage
# -----------------------------
def get_secret_value(key):
    value = os.getenv(key)
    if value:
        return value

    try:
        import streamlit as st
        return st.secrets.get(key)
    except Exception:
        return None


def get_google_sheet():
    google_sheet_id = get_secret_value("GOOGLE_SHEET_ID")
    service_account_json = get_secret_value("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not google_sheet_id or not service_account_json:
        return None

    if isinstance(service_account_json, str):
        service_account_info = json.loads(service_account_json)
    else:
        service_account_info = dict(service_account_json)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes,
    )

    client = gspread.authorize(credentials)
    return client.open_by_key(google_sheet_id)


def read_sheet(tab_name):
    sheet = get_google_sheet()

    if sheet is None:
        return pd.DataFrame()

    try:
        worksheet = sheet.worksheet(tab_name)
        records = worksheet.get_all_records()
        return pd.DataFrame(records)
    except Exception:
        return pd.DataFrame()


def write_sheet(tab_name, df):
    sheet = get_google_sheet()

    if sheet is None:
        return

    try:
        worksheet = sheet.worksheet(tab_name)
    except Exception:
        worksheet = sheet.add_worksheet(title=tab_name, rows=1000, cols=50)

    worksheet.clear()

    if df.empty:
        return

    df = df.fillna("").astype(str)
    worksheet.update([df.columns.tolist()] + df.values.tolist())


# -----------------------------
# General helpers
# -----------------------------
def safe_name(value: str) -> str:
    value = str(value or "unknown").strip()
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", value)
    return value[:60] or "unknown"


def clean_phone(phone: str) -> str:
    phone = str(phone or "")
    phone = phone.replace("whatsapp:", "")
    phone = phone.replace("+", "")
    phone = phone.replace(" ", "")
    phone = phone.replace("-", "")
    phone = phone.replace("(", "")
    phone = phone.replace(")", "")
    return phone.strip()


def hash_password(password: str) -> str:
    return hashlib.sha256(str(password).encode("utf-8")).hexdigest()


def normalize_vendor(vendor):
    return str(vendor or "").strip().lower()


def make_vendor_memory_key(user_phone, vendor):
    return f"{clean_phone(user_phone)}|{normalize_vendor(vendor)}"


# -----------------------------
# Local folder/image storage
# -----------------------------
def ensure_storage():
    os.makedirs(BASE_DIR, exist_ok=True)

    for folder in DEFAULT_FOLDERS:
        os.makedirs(os.path.join(BASE_DIR, folder), exist_ok=True)


def save_image_to_folder(
    image_bytes: bytes,
    folder: str,
    vendor: str,
    ext: str = "jpg",
    bill_date: str = "",
) -> str:
    ensure_storage()

    folder = folder if folder in DEFAULT_FOLDERS else "Uncategorized"
    folder_path = os.path.join(BASE_DIR, folder)
    os.makedirs(folder_path, exist_ok=True)

    clean_vendor = safe_name(vendor)

    try:
        date_obj = datetime.strptime(bill_date, "%Y-%m-%d")
        date_label = date_obj.strftime("%B_%d")
    except Exception:
        date_label = datetime.now().strftime("%B_%d")

    file_name = (
        f"{date_label}_{clean_vendor}_{safe_name(folder)}_bill_"
        f"{uuid.uuid4().hex[:6]}.{ext}"
    )
    image_path = os.path.join(folder_path, file_name)

    with open(image_path, "wb") as f:
        f.write(image_bytes)

    return image_path


def list_folder_images(folder: str):
    ensure_storage()

    folder_path = os.path.join(BASE_DIR, folder)

    if not os.path.exists(folder_path):
        return []

    valid_ext = (".png", ".jpg", ".jpeg", ".webp")

    return [
        os.path.join(folder_path, file)
        for file in os.listdir(folder_path)
        if file.lower().endswith(valid_ext)
    ]


# -----------------------------
# Entries / WhatsApp expenses
# -----------------------------
def load_entries():
    return read_sheet("entries")


def save_entries(df):
    write_sheet("entries", df)


def append_entry(entry):
    df = load_entries()
    new_row = pd.DataFrame([entry])

    if df.empty:
        df = new_row
    else:
        df = pd.concat([df, new_row], ignore_index=True)

    save_entries(df)


# -----------------------------
# Users / Login
# -----------------------------
def load_users():
    df = read_sheet("users")

    if df.empty:
        return pd.DataFrame(columns=["phone", "password_hash"])

    if "phone" not in df.columns:
        df["phone"] = ""

    if "password_hash" not in df.columns:
        df["password_hash"] = ""

    df["phone"] = df["phone"].astype(str).apply(clean_phone)

    return df


def save_users(df):
    write_sheet("users", df)


def register_user(phone: str, password: str):
    try:
        phone_clean = clean_phone(phone)

        if not phone_clean:
            return False, "Enter a valid phone number."

        if not password or len(str(password)) < 4:
            return False, "Password must be at least 4 characters."

        df = load_users()

        if phone_clean in df["phone"].astype(str).values:
            return False, "Account already exists. Please login."

        new_user = pd.DataFrame([
            {
                "phone": phone_clean,
                "password_hash": hash_password(password),
            }
        ])

        df = pd.concat([df, new_user], ignore_index=True)
        save_users(df)

        return True, "Account created successfully. Please login."

    except Exception as e:
        return False, f"Could not create account. Error: {str(e)}"


def validate_login(phone: str, password: str):
    try:
        phone_clean = clean_phone(phone)

        if not phone_clean or not password:
            return False

        df = load_users()

        if df.empty:
            return False

        df["phone"] = df["phone"].astype(str).apply(clean_phone)

        password_hash = hash_password(password)

        matched = df[
            (df["phone"].astype(str) == phone_clean) &
            (df["password_hash"].astype(str) == password_hash)
        ]

        return not matched.empty

    except Exception:
        return False


def reset_password(phone: str, new_password: str):
    try:
        phone_clean = clean_phone(phone)

        if not phone_clean:
            return False, "Enter a valid phone number."

        if not new_password or len(str(new_password)) < 4:
            return False, "Password must be at least 4 characters."

        df = load_users()

        if df.empty:
            return False, "Phone number not found."

        df["phone"] = df["phone"].astype(str).apply(clean_phone)

        if phone_clean not in df["phone"].astype(str).values:
            return False, "Phone number not found."

        df.loc[
            df["phone"].astype(str) == phone_clean,
            "password_hash"
        ] = hash_password(new_password)

        save_users(df)

        return True, "Password reset successfully. Please login."

    except Exception as e:
        return False, f"Could not reset password. Error: {str(e)}"


# -----------------------------
# Vendor memory
# -----------------------------
def load_vendor_rules():
    df = read_sheet("vendor_rules")

    if df.empty:
        return pd.DataFrame(
            columns=[
                "memory_key",
                "user_phone",
                "vendor",
                "category",
                "folder",
            ]
        )

    if "memory_key" not in df.columns:
        df["memory_key"] = df.apply(
            lambda row: make_vendor_memory_key(
                row.get("user_phone", ""),
                row.get("vendor", ""),
            ),
            axis=1,
        )

    return df


def save_vendor_rules(df):
    write_sheet("vendor_rules", df)


def update_vendor_memory(user_phone, vendor, category, folder):
    if not vendor or not category:
        return

    rules_df = load_vendor_rules()

    phone_clean = clean_phone(user_phone)
    vendor_clean = normalize_vendor(vendor)
    memory_key = make_vendor_memory_key(phone_clean, vendor_clean)

    new_rule = pd.DataFrame([
        {
            "memory_key": memory_key,
            "user_phone": phone_clean,
            "vendor": vendor_clean,
            "category": category,
            "folder": folder if folder else category,
        }
    ])

    if not rules_df.empty:
        rules_df = rules_df[
            rules_df["memory_key"].astype(str) != memory_key
        ]

    rules_df = pd.concat([rules_df, new_rule], ignore_index=True)

    save_vendor_rules(rules_df)


def apply_vendor_memory(user_phone, vendor):
    rules_df = load_vendor_rules()

    if rules_df.empty or not vendor:
        return None, None

    phone_clean = clean_phone(user_phone)
    vendor_clean = normalize_vendor(vendor)
    memory_key = make_vendor_memory_key(phone_clean, vendor_clean)

    match = rules_df[
        rules_df["memory_key"].astype(str) == memory_key
    ]

    if match.empty:
        return None, None

    row = match.iloc[-1]

    return row.get("category", None), row.get("folder", None)


# -----------------------------
# Petpooja
# -----------------------------
def load_petpooja_entries():
    return read_sheet("petpooja_entries")


def save_petpooja_entries(df):
    write_sheet("petpooja_entries", df)