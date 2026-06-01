import os
import re
import uuid
import pandas as pd
from datetime import datetime
import os
import pandas as pd
import hashlib



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


def safe_name(value: str) -> str:
    value = str(value or "unknown").strip()
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", value)
    return value[:60] or "unknown"


def ensure_storage():
    os.makedirs(BASE_DIR, exist_ok=True)

    for folder in DEFAULT_FOLDERS:
        os.makedirs(os.path.join(BASE_DIR, folder), exist_ok=True)

    if not os.path.exists(CSV_PATH):
        df = pd.DataFrame(columns=[
            "id",
            "date",
            "transaction_type",
            "vendor",
            "user_phone",
            "description",
            "category",
            "folder",
            "subtotal",
            "tax",
            "total",
            "currency",
            "confidence",
            "reason",
            "image_path",
            "created_at",
        ])
        df.to_csv(CSV_PATH, index=False)


def save_image_to_folder(image_bytes: bytes, folder: str, vendor: str, ext: str = "jpg", bill_date: str = "") -> str:
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

    file_name = f"{date_label}_{clean_vendor}_{safe_name(folder)}_bill_{uuid.uuid4().hex[:6]}.{ext}"
    image_path = os.path.join(folder_path, file_name)

    with open(image_path, "wb") as f:
        f.write(image_bytes)

    return image_path


def append_entry(entry: dict):
    ensure_storage()

    df = pd.read_csv(CSV_PATH)

    entry["id"] = uuid.uuid4().hex
    entry["created_at"] = datetime.now().isoformat(timespec="seconds")

    df = pd.concat([df, pd.DataFrame([entry])], ignore_index=True)
    df.to_csv(CSV_PATH, index=False)


def load_entries() -> pd.DataFrame:
    ensure_storage()
    return pd.read_csv(CSV_PATH)


def save_entries(df: pd.DataFrame):
    ensure_storage()
    df.to_csv(CSV_PATH, index=False)


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

USERS_PATH = os.path.join(BASE_DIR, "users.csv")


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


def ensure_users_file():
    os.makedirs(BASE_DIR, exist_ok=True)

    if not os.path.exists(USERS_PATH):
        pd.DataFrame(columns=["phone", "password_hash"]).to_csv(
            USERS_PATH,
            index=False
        )


def load_users():
    ensure_users_file()

    try:
        df = pd.read_csv(USERS_PATH)
    except Exception:
        df = pd.DataFrame(columns=["phone", "password_hash"])

    if "phone" not in df.columns:
        df["phone"] = ""

    if "password_hash" not in df.columns:
        df["password_hash"] = ""

    df["phone"] = df["phone"].astype(str).apply(clean_phone)

    return df


def save_users(df):
    ensure_users_file()
    df.to_csv(USERS_PATH, index=False)


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

        password_hash = hash_password(password)

        matched = df[
            (df["phone"].astype(str) == phone_clean) &
            (df["password_hash"].astype(str) == password_hash)
        ]

        return not matched.empty

    except Exception:
        return False

def normalize_vendor(vendor):
    return str(vendor).strip().lower()

def reset_password(phone: str, new_password: str):
    try:
        phone_clean = clean_phone(phone)

        if not phone_clean:
            return False, "Enter a valid phone number."

        if not new_password or len(str(new_password)) < 4:
            return False, "Password must be at least 4 characters."

        df = load_users()

        if phone_clean not in df["phone"].astype(str).values:
            return False, "No account found for this phone number."

        df.loc[
            df["phone"].astype(str) == phone_clean,
            "password_hash"
        ] = hash_password(new_password)

        save_users(df)

        return True, "Password reset successfully. Please login."

    except Exception as e:
        return False, f"Could not reset password. Error: {str(e)}"


def validate_login(phone: str, password: str):
    ensure_users_file()

    phone_clean = clean_phone(phone)
    password_hash = hash_password(password)

    df = pd.read_csv(USERS_PATH)

    matched = df[
        (df["phone"].astype(str) == phone_clean) &
        (df["password_hash"].astype(str) == password_hash)
    ]

    return not matched.empty


def reset_password(phone: str, new_password: str):
    ensure_users_file()

    phone_clean = clean_phone(phone)
    df = pd.read_csv(USERS_PATH)

    if phone_clean not in df["phone"].astype(str).values:
        return False, "Phone number not found."

    df.loc[
        df["phone"].astype(str) == phone_clean,
        "password_hash"
    ] = hash_password(new_password)

    df.to_csv(USERS_PATH, index=False)

    return True, "Password reset successfully. Please login."


PETPOOJA_FILE = "data/petpooja_sales.csv"
VENDOR_RULES_FILE = "data/vendor_rules.csv"


def normalize_vendor(vendor):
    return str(vendor or "").strip().lower()


def make_vendor_memory_key(user_phone, vendor):
    return f"{clean_phone(user_phone)}|{normalize_vendor(vendor)}"


def load_vendor_rules():
    ensure_storage()

    if not os.path.exists(VENDOR_RULES_FILE):
        return pd.DataFrame(
            columns=[
                "memory_key",
                "user_phone",
                "vendor",
                "category",
                "folder",
            ]
        )

    df = pd.read_csv(VENDOR_RULES_FILE)

    if "memory_key" not in df.columns:
        df["memory_key"] = df.apply(
            lambda row: make_vendor_memory_key(
                row.get("user_phone", ""),
                row.get("vendor", "")
            ),
            axis=1
        )

    return df


def save_vendor_rules(df):
    ensure_storage()
    df.to_csv(VENDOR_RULES_FILE, index=False)


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
    ensure_storage()

    if not os.path.exists(PETPOOJA_FILE):
        return pd.DataFrame()

    return pd.read_csv(PETPOOJA_FILE)


def save_petpooja_entries(df):
    ensure_storage()
    df.to_csv(PETPOOJA_FILE, index=False)
    