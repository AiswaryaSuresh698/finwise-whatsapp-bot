import os
import re
import uuid
import hashlib
from datetime import datetime

import boto3
import pandas as pd
from sqlalchemy import create_engine, text, inspect
from sqlalchemy import text


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


def update_entry_by_id(entry_id, category=None, amount=None, vendor=None):
    updates = []
    params = {"id": str(entry_id)}

    if category is not None:
        updates.append("category = :category")
        updates.append("folder = :category")
        params["category"] = str(category)

    if amount is not None:
        updates.append("total = :amount")
        updates.append("subtotal = :amount")
        params["amount"] = str(amount)

    if vendor is not None:
        updates.append("vendor = :vendor")
        params["vendor"] = str(vendor)

    if not updates:
        return 0

    with engine.begin() as conn:
        result = conn.execute(
            text(f"""
                UPDATE entries
                SET {", ".join(updates)}
                WHERE id = :id
            """),
            params
        )

    return result.rowcount


def delete_entry_by_id(entry_id):
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                DELETE FROM entries
                WHERE id = :id
            """),
            {"id": str(entry_id)}
        )

    return result.rowcount

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
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_entries_user_phone ON entries(user_phone);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_entries_user_phone_date ON entries(user_phone, date);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_entries_is_deleted ON entries(is_deleted);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_petpooja_user_phone ON petpooja_entries(user_phone);"))

        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS business_profiles (
            owner_phone TEXT PRIMARY KEY,
            business_name TEXT,
            owner_name TEXT,
            business_type TEXT,
            business_email TEXT,
            currency TEXT,
            timezone TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """))

        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS financial_todos (
            id BIGSERIAL PRIMARY KEY,
            owner_phone TEXT,
            title TEXT,
            todo_type TEXT,
            due_date TEXT,
            amount TEXT,
            notes TEXT,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """))

        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_financial_todos_owner ON financial_todos(owner_phone);"))
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS user_categories (
            id BIGSERIAL PRIMARY KEY,
            owner_phone TEXT,
            category_name TEXT,
            active TEXT DEFAULT 'yes',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """))

        conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_user_categories_owner
        ON user_categories(owner_phone);
        """))


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
    if engine is None:
        return pd.DataFrame()

    try:
        init_db()

        phone_clean = clean_phone(user_phone)

        query = """
            SELECT *
            FROM entries
            WHERE COALESCE(user_phone, '') = :user_phone
            AND COALESCE(is_deleted, '') != 'yes'
            ORDER BY
                CASE
                    WHEN date ~ '^\\d{4}-\\d{2}-\\d{2}$' THEN date::date
                    ELSE NULL
                END DESC NULLS LAST,
                id::bigint DESC NULLS LAST
            LIMIT :limit
        """

        return pd.read_sql(
            text(query),
            engine,
            params={
                "user_phone": phone_clean,
                "limit": int(limit),
            }
        )

    except Exception as e:
        print("load_entries_for_user error:", str(e))
        return pd.DataFrame()



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

from datetime import datetime
import pandas as pd

def normalize_date_ddmmyyyy(value):
    value = str(value or "").strip()

    if not value:
        return datetime.now().strftime("%Y-%m-%d")

    # Always treat slash dates as DD/MM/YYYY
    if "/" in value:
        parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
    else:
        parsed = pd.to_datetime(value, errors="coerce", dayfirst=False)

    if pd.isna(parsed):
        return datetime.now().strftime("%Y-%m-%d")

    return parsed.strftime("%Y-%m-%d")


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

def delete_entries_by_ids(entry_ids):
    ids = [str(x).strip() for x in entry_ids if str(x).strip()]

    if not ids:
        return 0

    with engine.begin() as conn:
        result = conn.execute(
            text("""
                DELETE FROM entries
                WHERE id = ANY(:ids)
            """),
            {"ids": ids}
        )

    return result.rowcount

def get_business_profile(owner_phone):
    phone_clean = clean_phone(owner_phone)

    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT *
                FROM business_profiles
                WHERE owner_phone = :owner_phone
                LIMIT 1
            """),
            {"owner_phone": phone_clean}
        ).mappings().fetchone()

    return dict(row) if row else {}


def upsert_business_profile(owner_phone, business_name, owner_name, business_type, business_email, currency, timezone):
    phone_clean = clean_phone(owner_phone)

    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO business_profiles (
                    owner_phone, business_name, owner_name, business_type,
                    business_email, currency, timezone, updated_at
                )
                VALUES (
                    :owner_phone, :business_name, :owner_name, :business_type,
                    :business_email, :currency, :timezone, CURRENT_TIMESTAMP
                )
                ON CONFLICT (owner_phone)
                DO UPDATE SET
                    business_name = EXCLUDED.business_name,
                    owner_name = EXCLUDED.owner_name,
                    business_type = EXCLUDED.business_type,
                    business_email = EXCLUDED.business_email,
                    currency = EXCLUDED.currency,
                    timezone = EXCLUDED.timezone,
                    updated_at = CURRENT_TIMESTAMP
            """),
            {
                "owner_phone": phone_clean,
                "business_name": business_name,
                "owner_name": owner_name,
                "business_type": business_type,
                "business_email": business_email,
                "currency": currency,
                "timezone": timezone,
            }
        )


def add_restaurant_uploader(owner_phone, uploader_phone, uploader_name):
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO restaurant_uploaders (
                    owner_phone, uploader_phone, uploader_name, active
                )
                VALUES (
                    :owner_phone, :uploader_phone, :uploader_name, 'yes'
                )
            """),
            {
                "owner_phone": clean_phone(owner_phone),
                "uploader_phone": clean_phone(uploader_phone),
                "uploader_name": str(uploader_name or "").strip(),
            }
        )


def deactivate_restaurant_uploader(uploader_id):
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                UPDATE restaurant_uploaders
                SET active = 'no'
                WHERE id = :id
            """),
            {"id": str(uploader_id)}
        )

    return result.rowcount


def load_financial_todos(owner_phone):
    phone_clean = clean_phone(owner_phone)

    try:
        return pd.read_sql(
            text("""
                SELECT *
                FROM financial_todos
                WHERE owner_phone = :owner_phone
                ORDER BY
                    CASE
                        WHEN due_date ~ '^\\d{4}-\\d{2}-\\d{2}$' THEN due_date::date
                        ELSE NULL
                    END ASC NULLS LAST,
                    id DESC
            """),
            engine,
            params={"owner_phone": phone_clean}
        )
    except Exception as e:
        print("load_financial_todos error:", str(e))
        return pd.DataFrame()


def add_financial_todo(owner_phone, title, todo_type, due_date, amount, notes):
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO financial_todos (
                    owner_phone, title, todo_type, due_date, amount, notes, status
                )
                VALUES (
                    :owner_phone, :title, :todo_type, :due_date, :amount, :notes, 'open'
                )
            """),
            {
                "owner_phone": clean_phone(owner_phone),
                "title": str(title or "").strip(),
                "todo_type": str(todo_type or "").strip(),
                "due_date": str(due_date or "").strip(),
                "amount": str(amount or "").strip(),
                "notes": str(notes or "").strip(),
            }
        )


def update_financial_todo_status(todo_id, status):
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                UPDATE financial_todos
                SET status = :status
                WHERE id = :id
            """),
            {
                "id": str(todo_id),
                "status": str(status or "open").strip(),
            }
        )

    return result.rowcount


def delete_financial_todo(todo_id):
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                DELETE FROM financial_todos
                WHERE id = :id
            """),
            {"id": str(todo_id)}
        )

    return result.rowcount

def load_user_categories(owner_phone):
    phone_clean = clean_phone(owner_phone)

    df = pd.read_sql(
        text("""
            SELECT *
            FROM user_categories
            WHERE owner_phone = :owner_phone
            AND COALESCE(active, 'yes') = 'yes'
            ORDER BY category_name ASC
        """),
        engine,
        params={"owner_phone": phone_clean}
    )

    if df.empty:
        return pd.DataFrame(columns=["id", "category_name"])

    return df


def add_user_category(owner_phone, category_name):
    category = str(category_name or "").strip().title()

    if not category:
        return 0

    with engine.begin() as conn:
        existing = conn.execute(
            text("""
                SELECT id
                FROM user_categories
                WHERE owner_phone = :owner_phone
                AND LOWER(category_name) = LOWER(:category_name)
                AND COALESCE(active, 'yes') = 'yes'
                LIMIT 1
            """),
            {
                "owner_phone": clean_phone(owner_phone),
                "category_name": category,
            }
        ).fetchone()

        if existing:
            return 0

        conn.execute(
            text("""
                INSERT INTO user_categories (owner_phone, category_name, active)
                VALUES (:owner_phone, :category_name, 'yes')
            """),
            {
                "owner_phone": clean_phone(owner_phone),
                "category_name": category,
            }
        )

    return 1


def delete_user_category(category_id):
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                UPDATE user_categories
                SET active = 'no'
                WHERE id = :id
            """),
            {"id": str(category_id)}
        )

    return result.rowcount

def load_petpooja_entries_for_user(phone, limit=1000):
    if engine is None:
        return pd.DataFrame()

    try:
        phone_clean = clean_phone(phone)

        return pd.read_sql(
            text("""
                SELECT *
                FROM petpooja_entries
                WHERE user_phone = :user_phone
                ORDER BY id DESC
                LIMIT :limit
            """),
            engine,
            params={
                "user_phone": phone_clean,
                "limit": int(limit),
            }
        )

    except Exception as e:
        print("load_petpooja_entries_for_user error:", str(e))
        return pd.DataFrame()


def get_dashboard_totals_for_user(phone, start_date=None, end_date=None):
    if engine is None:
        return 0.0, 0.0

    try:
        phone_clean = clean_phone(phone)

        date_sql = ""
        params = {"user_phone": phone_clean}

        if start_date and end_date:
            date_sql = """
                AND CASE
                    WHEN date ~ '^\\d{4}-\\d{2}-\\d{2}$'
                    THEN date::date
                    ELSE NULL
                END BETWEEN :start_date AND :end_date
            """
            params["start_date"] = start_date
            params["end_date"] = end_date

        query = text(f"""
            SELECT
                COALESCE(SUM(
                    CASE
                        WHEN LOWER(COALESCE(transaction_type, '')) = 'income'
                        THEN COALESCE(NULLIF(total, ''), '0')::numeric
                        ELSE 0
                    END
                ), 0) AS whatsapp_income,

                COALESCE(SUM(
                    CASE
                        WHEN LOWER(COALESCE(transaction_type, '')) = 'expense'
                        THEN COALESCE(NULLIF(total, ''), '0')::numeric
                        ELSE 0
                    END
                ), 0) AS total_expense
            FROM entries
            WHERE user_phone = :user_phone
            AND COALESCE(is_deleted, '') != 'yes'
            AND LOWER(COALESCE(source, '')) != 'petpooja'
            {date_sql}
        """)

        df = pd.read_sql(query, engine, params=params)

        if df.empty:
            return 0.0, 0.0

        return (
            float(df.iloc[0]["whatsapp_income"] or 0),
            float(df.iloc[0]["total_expense"] or 0),
        )

    except Exception as e:
        print("get_dashboard_totals_for_user error:", str(e))
        return 0.0, 0.0


def get_petpooja_total_for_user(phone, start_date=None, end_date=None):
    if engine is None:
        return 0.0

    try:
        phone_clean = clean_phone(phone)

        date_sql = ""
        params = {"user_phone": phone_clean}

        if start_date and end_date:
            date_sql = """
                AND CASE
                    WHEN "Date" ~ '^\\d{4}-\\d{2}-\\d{2}$'
                    THEN "Date"::date
                    WHEN "date" ~ '^\\d{4}-\\d{2}-\\d{2}$'
                    THEN "date"::date
                    ELSE NULL
                END BETWEEN :start_date AND :end_date
            """
            params["start_date"] = start_date
            params["end_date"] = end_date

        df = pd.read_sql(
            text(f"""
                SELECT COALESCE(SUM(
                    COALESCE(NULLIF(total, ''), NULLIF("Total", ''), '0')::numeric
                ), 0) AS petpooja_total
                FROM petpooja_entries
                WHERE user_phone = :user_phone
                {date_sql}
            """),
            engine,
            params=params
        )

        if df.empty:
            return 0.0

        return float(df.iloc[0]["petpooja_total"] or 0)

    except Exception as e:
        print("get_petpooja_total_for_user error:", str(e))
        return 0.0