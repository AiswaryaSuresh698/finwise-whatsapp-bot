import os
import re
import uuid
import hashlib
from datetime import datetime

import boto3
import pandas as pd
from sqlalchemy import create_engine, text, inspect


BASE_DIR = "finwise_storage"

DEFAULT_FOLDERS = [
    "Grocery", "Gas", "Internet", "Utilities", "Meals", "Rent",
    "Software", "Salary", "Office Supplies", "Vehicle",
    "Professional Fees", "Insurance", "Travel", "Income",
    "Uncategorized", "Milk", "Chicken", "Rice", "Brownie", "Egg",
    "Butter", "Soap Oil", "Cylinder", "Frozen", "Ice Cream",
    "Parotta", "Marketing",
]


def get_secret_value(key):
    value = os.getenv(key)
    if value:
        return value

    try:
        import streamlit as st
        return st.secrets.get(key)
    except Exception:
        return None


DATABASE_URL = get_secret_value("DATABASE_URL")
AWS_REGION = get_secret_value("AWS_REGION") or "ap-south-1"
S3_BUCKET_NAME = get_secret_value("S3_BUCKET_NAME")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
) if DATABASE_URL else None


def get_s3_client():
    if not S3_BUCKET_NAME:
        return None

    return boto3.client(
        "s3",
        region_name=AWS_REGION,
        aws_access_key_id=get_secret_value("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=get_secret_value("AWS_SECRET_ACCESS_KEY"),
    )


s3 = get_s3_client()


def _table_name(tab_name: str) -> str:
    name = str(tab_name or "").strip().lower()
    name = re.sub(r"[^a-z0-9_]+", "_", name).strip("_")
    if not name:
        raise ValueError("Invalid table name")
    return name


def _sanitize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    clean_cols = []
    seen = {}

    for col in df.columns:
        clean = str(col).strip().lower()
        clean = re.sub(r"[^a-z0-9_]+", "_", clean).strip("_") or "column"

        if clean in seen:
            seen[clean] += 1
            clean = f"{clean}_{seen[clean]}"
        else:
            seen[clean] = 1

        clean_cols.append(clean)

    df.columns = clean_cols
    return df


def init_db():
    if engine is None:
        raise RuntimeError("DATABASE_URL is missing.")

    with engine.begin() as conn:
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS entries (
            id BIGSERIAL PRIMARY KEY,
            from_number TEXT,
            transaction_type TEXT,
            date TEXT,
            vendor TEXT,
            description TEXT,
            category TEXT,
            folder TEXT,
            subtotal TEXT,
            tax TEXT,
            total TEXT,
            currency TEXT,
            payment_method TEXT,
            confidence TEXT,
            reason TEXT,
            image_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """))

        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            phone TEXT UNIQUE,
            password_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """))

        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS vendor_rules (
            id BIGSERIAL PRIMARY KEY,
            memory_key TEXT UNIQUE,
            user_phone TEXT,
            vendor TEXT,
            category TEXT,
            folder TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """))

        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS petpooja_entries (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """))

        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS restaurant_uploaders (
            id BIGSERIAL PRIMARY KEY,
            owner_phone TEXT,
            uploader_phone TEXT,
            uploader_name TEXT,
            active TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """))

        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_entries_from_number ON entries(from_number);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(date);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_entries_vendor ON entries(vendor);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vendor_rules_key ON vendor_rules(memory_key);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_uploaders_phone ON restaurant_uploaders(uploader_phone);"))


def _existing_columns(table_name: str):
    inspector = inspect(engine)
    if not inspector.has_table(table_name):
        return []
    return [col["name"] for col in inspector.get_columns(table_name)]


def _ensure_table_columns(table_name: str, df: pd.DataFrame):
    if df.empty:
        return

    existing = set(_existing_columns(table_name))

    with engine.begin() as conn:
        for col in df.columns:
            if col not in existing:
                conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{col}" TEXT;'))


def read_sheet(tab_name):
    if engine is None:
        return pd.DataFrame()

    table = _table_name(tab_name)

    try:
        init_db()
        return pd.read_sql(f'SELECT * FROM "{table}" ORDER BY id DESC', engine)
    except Exception as e:
        print(f"read_sheet error for {table}: {e}")
        return pd.DataFrame()


def write_sheet(tab_name, df):
    if engine is None:
        return

    table = _table_name(tab_name)

    try:
        init_db()
        df = pd.DataFrame(df)

        if df.empty:
            return

        df = _sanitize_columns(df)
        df = df.fillna("").astype(str)

        df.to_sql(table, engine, if_exists="replace", index=False)

        with engine.begin() as conn:
            conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS id BIGSERIAL PRIMARY KEY;'))

    except Exception as e:
        print(f"write_sheet error for {table}: {e}")


def append_sheet_row(tab_name, row: dict):
    if engine is None:
        return

    table = _table_name(tab_name)

    try:
        init_db()

        df = pd.DataFrame([row])
        df = _sanitize_columns(df)
        df = df.fillna("").astype(str)

        _ensure_table_columns(table, df)

        df.to_sql(table, engine, if_exists="append", index=False)

    except Exception as e:
        print(f"append_sheet_row error for {table}: {e}")


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
    phone = phone.strip()

    if phone.endswith(".0"):
        phone = phone[:-2]

    if len(phone) == 10 and phone.startswith(("6", "7", "8", "9")):
        phone = "91" + phone

    return phone


def hash_password(password: str) -> str:
    return hashlib.sha256(str(password).encode("utf-8")).hexdigest()


def normalize_vendor(vendor):
    return str(vendor or "").strip().lower()


def make_vendor_memory_key(user_phone, vendor):
    return f"{clean_phone(user_phone)}|{normalize_vendor(vendor)}"


def ensure_storage():
    os.makedirs(BASE_DIR, exist_ok=True)


def save_image_to_folder(
    image_bytes: bytes,
    folder: str,
    vendor: str,
    ext: str = "jpg",
    bill_date: str = "",
) -> str:
    folder = folder if folder in DEFAULT_FOLDERS else "Uncategorized"
    clean_vendor = safe_name(vendor)

    try:
        date_obj = datetime.strptime(str(bill_date), "%Y-%m-%d")
        date_label = date_obj.strftime("%B_%d")
        year_month = date_obj.strftime("%Y/%m")
    except Exception:
        date_label = datetime.now().strftime("%B_%d")
        year_month = datetime.now().strftime("%Y/%m")

    file_name = (
        f"{date_label}_{clean_vendor}_{safe_name(folder)}_bill_"
        f"{uuid.uuid4().hex[:6]}.{ext}"
    )

    if s3 and S3_BUCKET_NAME:
        key = f"receipts/{folder}/{year_month}/{file_name}"

        content_type = "image/jpeg"
        if ext.lower() == "png":
            content_type = "image/png"
        elif ext.lower() == "webp":
            content_type = "image/webp"

        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=key,
            Body=image_bytes,
            ContentType=content_type,
        )

        return f"s3://{S3_BUCKET_NAME}/{key}"

    ensure_storage()
    folder_path = os.path.join(BASE_DIR, folder)
    os.makedirs(folder_path, exist_ok=True)

    image_path = os.path.join(folder_path, file_name)

    with open(image_path, "wb") as f:
        f.write(image_bytes)

    return image_path


def get_presigned_s3_url(s3_path: str, expires_in: int = 3600):
    if not s3_path or not str(s3_path).startswith("s3://"):
        return s3_path

    if not s3:
        return s3_path

    without_scheme = s3_path.replace("s3://", "", 1)
    bucket, key = without_scheme.split("/", 1)

    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def list_folder_images(folder: str):
    folder = folder if folder in DEFAULT_FOLDERS else "Uncategorized"

    if s3 and S3_BUCKET_NAME:
        prefix = f"receipts/{folder}/"
        response = s3.list_objects_v2(Bucket=S3_BUCKET_NAME, Prefix=prefix)

        files = []

        for obj in response.get("Contents", []):
            key = obj.get("Key")

            if key and key.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                files.append(get_presigned_s3_url(f"s3://{S3_BUCKET_NAME}/{key}"))

        return files

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

def load_entries_for_user(user_phone, limit=500):
    df = load_entries()

    if df.empty:
        return df

    if "user_phone" not in df.columns:
        df["user_phone"] = ""

    phone_clean = clean_phone(user_phone)
    df["user_phone_clean"] = df["user_phone"].astype(str).apply(clean_phone)

    df = df[df["user_phone_clean"] == phone_clean].copy()

    if "is_deleted" in df.columns:
        df = df[df["is_deleted"].astype(str).str.lower() != "yes"].copy()

    if "date" in df.columns:
        df["date_sort"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.sort_values("date_sort", ascending=False)
        df = df.drop(columns=["date_sort"], errors="ignore")

    if limit:
        df = df.head(int(limit))

    return df


def load_entries():
    return read_sheet("entries")


def save_entries(df):
    write_sheet("entries", df)


def append_entry(entry):
    append_sheet_row("entries", entry)


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

        if not df.empty and phone_clean in df["phone"].astype(str).values:
            return False, "Account already exists. Please login."

        append_sheet_row("users", {
            "phone": phone_clean,
            "password_hash": hash_password(password),
        })

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


def load_petpooja_entries():
    return read_sheet("petpooja_entries")


def save_petpooja_entries(df):
    write_sheet("petpooja_entries", df)


def append_petpooja_entry(entry):
    append_sheet_row("petpooja_entries", entry)


def load_restaurant_uploaders():
    df = read_sheet("restaurant_uploaders")

    if df.empty:
        return pd.DataFrame(columns=[
            "owner_phone",
            "uploader_phone",
            "uploader_name",
            "active",
        ])

    df.columns = [
        str(col).strip().lower().replace(" ", "_")
        for col in df.columns
    ]

    for col in ["owner_phone", "uploader_phone", "uploader_name", "active"]:
        if col not in df.columns:
            df[col] = ""

    df["owner_phone"] = df["owner_phone"].astype(str).apply(clean_phone)
    df["uploader_phone"] = df["uploader_phone"].astype(str).apply(clean_phone)
    df["uploader_name"] = df["uploader_name"].astype(str).str.strip()
    df["active"] = df["active"].astype(str).str.lower().str.strip()

    return df


def save_restaurant_uploaders(df):
    write_sheet("restaurant_uploaders", df)


def get_owner_phone_for_uploader(uploader_phone):
    uploader_clean = clean_phone(uploader_phone)

    df = load_restaurant_uploaders()

    if df.empty:
        return uploader_clean

    df["active_match"] = df["active"].astype(str).str.lower().str.strip()

    matched = df[
        (df["uploader_phone"].astype(str) == uploader_clean) &
        (df["active_match"].isin(["yes", "true", "1", "active"]))
    ]

    if matched.empty:
        print("No restaurant uploader match found for:", uploader_clean)
        print("Available uploaders:", df["uploader_phone"].tolist())
        return None

    return clean_phone(matched.iloc[-1]["owner_phone"])