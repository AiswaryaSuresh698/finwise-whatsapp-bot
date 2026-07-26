import os
import random
import time
from io import BytesIO
from datetime import date, timedelta
import uuid
import json
from altair import value
from openai import OpenAI

import pandas as pd
import qrcode
import streamlit as st
import streamlit.components.v1 as components
from twilio.rest import Client
from dotenv import load_dotenv
import calendar
from sqlalchemy import text
import matplotlib.pyplot as plt

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
    load_user_categories,
    add_user_category,
    delete_user_category,
    load_petpooja_entries_for_user,
    get_dashboard_totals_for_user,
    get_petpooja_total_for_user,
    soft_delete_entries_by_ids,
    load_recently_deleted_entries,
    restore_deleted_entry,
    permanently_delete_entry,
    purge_expired_deleted_entries,
    get_entry_by_reference,
    soft_delete_entry,
    append_income_entry,
    load_income_entries_for_user,
    get_manual_income_total_for_user,
    get_monthly_analysis_for_user,
    get_month_expense_breakdown_for_user,
    compare_expense_categories_for_user,
    update_income_entry_by_id,
    soft_delete_income_entry,
    engine,
    load_petpooja_reports_for_user,
    delete_petpooja_report,
    append_petpooja_report,
    normalize_category,
    upsert_category_budget,
    load_category_budgets,
    delete_category_budget,
)

load_dotenv()

def get_currency_symbol(phone_number):
    """
    +1  -> $
    +91 -> ₹

    Also works when the stored phone number does not include '+'.
    """
    phone = str(phone_number or "").strip()

    # Remove WhatsApp prefix if present
    phone = phone.replace("whatsapp:", "")

    # Keep digits only
    digits = "".join(char for char in phone if char.isdigit())

    if digits.startswith("91"):
        return "₹"

    if digits.startswith("1"):
        return "$"

    # Default currency
    return "₹"


def get_currency_code(phone_number):
    """Return the storage currency code for the logged-in phone."""
    phone = str(phone_number or "").replace("whatsapp:", "").strip()
    digits = "".join(char for char in phone if char.isdigit())

    if digits.startswith("1"):
        return "CAD"

    return "INR"


def get_currency_column_format(phone_number):
    """Return the Streamlit NumberColumn format for the user currency."""
    return f"{get_currency_symbol(phone_number)}%.2f"


def format_currency(amount, phone_number, decimals=2):
    symbol = get_currency_symbol(phone_number)

    try:
        value = float(amount or 0)
    except (TypeError, ValueError):
        value = 0.0

    return f"{symbol}{value:,.{decimals}f}"


def ensure_feedback_table():
    """Create the feedback table only when the feedback page is used."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS finwise_feedback (
                id SERIAL PRIMARY KEY,
                user_phone TEXT NOT NULL,
                rating INTEGER,
                feature_request TEXT,
                improvement TEXT,
                contact_permission BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))


def save_finwise_feedback(
    user_phone,
    rating,
    feature_request,
    improvement,
    contact_permission,
):
    ensure_feedback_table()

    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO finwise_feedback (
                    user_phone,
                    rating,
                    feature_request,
                    improvement,
                    contact_permission
                )
                VALUES (
                    :user_phone,
                    :rating,
                    :feature_request,
                    :improvement,
                    :contact_permission
                )
            """),
            {
                "user_phone": clean_phone(user_phone),
                "rating": int(rating),
                "feature_request": feature_request.strip(),
                "improvement": improvement.strip(),
                "contact_permission": bool(contact_permission),
            },
        )


def render_autofill_sync():
    """Ask the browser to sync password-manager autofill with Streamlit."""
    components.html(
        """
        <script>
        function syncAutofill() {
            const doc = window.parent.document;
            const inputs = doc.querySelectorAll(
                'input[autocomplete="tel"], input[autocomplete="current-password"]'
            );

            inputs.forEach((input) => {
                if (input.value) {
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    input.blur();
                }
            });
        }

        setTimeout(syncAutofill, 250);
        setTimeout(syncAutofill, 700);
        setTimeout(syncAutofill, 1400);
        </script>
        """,
        height=0,
    )

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

@st.cache_resource(show_spinner=False)
def check_database_connection():
    """
    Checks PostgreSQL connectivity once per Streamlit process.
    It does not create or alter database tables.
    """
    if engine is None:
        return False, "DATABASE_URL is missing."

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        return True, ""

    except Exception as exc:
        return False, str(exc)


database_ready, database_error = check_database_connection()

if not database_ready:
    st.error(
        "FinWise cannot connect to the database right now."
    )

    print(
        "DATABASE CONNECTION ERROR:",
        database_error,
        flush=True,
    )

    st.stop()

@st.cache_data(ttl=300, show_spinner=False)
def cached_load_entries():
    return load_entries()

@st.cache_data(
    ttl=180,
    show_spinner=False,
)
def cached_load_entries_for_user(
    phone,
    limit=100,
):
    return load_entries_for_user(
        phone,
        limit,
    )
    

@st.cache_data(ttl=300, show_spinner=False)
def cached_load_petpooja_entries_for_user(phone, limit=1000):
    return load_petpooja_entries_for_user(phone, limit)

@st.cache_data(ttl=300, show_spinner=False)
def cached_load_petpooja_entries():
    return load_petpooja_entries()

@st.cache_data(ttl=120, show_spinner=False)
def cached_dashboard_totals(phone, start_date=None, end_date=None):
    return get_dashboard_totals_for_user(phone, start_date, end_date)

@st.cache_data(ttl=120, show_spinner=False)
def cached_petpooja_total(phone, start_date=None, end_date=None):
    return get_petpooja_total_for_user(phone, start_date, end_date)

ensure_storage()

@st.cache_data(ttl=120, show_spinner=False)
def cached_manual_income_total(
    phone,
    start_date=None,
    end_date=None
):
    return get_manual_income_total_for_user(
        phone,
        start_date,
        end_date
    )


@st.cache_data(ttl=300, show_spinner=False)
def cached_income_entries(
    phone,
    start_date=None,
    end_date=None,
    limit=500
):
    return load_income_entries_for_user(
        phone=phone,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )

@st.cache_data(ttl=300, show_spinner=False)
def cached_monthly_analysis(phone, year):
    return get_monthly_analysis_for_user(
        phone=phone,
        year=year,
    )


@st.cache_data(ttl=300, show_spinner=False)
def cached_month_expense_breakdown(
    phone,
    year,
    month
):
    return get_month_expense_breakdown_for_user(
        phone=phone,
        year=year,
        month=month,
    )


@st.cache_data(ttl=300, show_spinner=False)
def cached_month_category_comparison(
    phone,
    year,
    base_month,
    comparison_month
):
    return compare_expense_categories_for_user(
        phone=phone,
        year=year,
        base_month=base_month,
        comparison_month=comparison_month,
    )



# -----------------------------
# Helpers
# -----------------------------
def read_petpooja_file(uploaded_file):
    """
    Reads:
    - Genuine Excel 97-2003 .xls files
    - Modern .xlsx files
    - Petpooja HTML reports saved with an .xls extension
    """

    file_name = uploaded_file.name.lower().strip()
    file_bytes = uploaded_file.getvalue()

    if not file_bytes:
        raise Exception("The uploaded Petpooja file is empty.")

    try:
        # Modern XLSX file
        # XLSX files are ZIP files and normally begin with PK.
        if file_bytes.startswith(b"PK"):
            return pd.read_excel(
                BytesIO(file_bytes),
                engine="openpyxl",
                header=None,
            )

        # Genuine Microsoft Excel 97-2003 binary XLS file
        if file_bytes.startswith(b"\xD0\xCF\x11\xE0"):
            return pd.read_excel(
                BytesIO(file_bytes),
                engine="xlrd",
                header=None,
            )

        # Petpooja often exports an HTML table with an .xls extension
        file_start = file_bytes[:10000].lower()

        if (
            b"<html" in file_start
            or b"<table" in file_start
            or b"<!doctype html" in file_start
        ):
            tables = pd.read_html(
                BytesIO(file_bytes),
                flavor="lxml",
                header=None,
            )

            if not tables:
                raise Exception(
                    "No table was found in the Petpooja report."
                )

            # Return the largest table in the report
            return max(
                tables,
                key=lambda table: table.shape[0] * table.shape[1]
            )

        # Extension-based fallback
        if file_name.endswith(".xlsx"):
            return pd.read_excel(
                BytesIO(file_bytes),
                engine="openpyxl",
                header=None,
            )

        if file_name.endswith(".xls"):
            try:
                return pd.read_excel(
                    BytesIO(file_bytes),
                    engine="xlrd",
                    header=None,
                )
            except Exception:
                tables = pd.read_html(
                    BytesIO(file_bytes),
                    flavor="lxml",
                    header=None,
                )

                if tables:
                    return max(
                        tables,
                        key=lambda table: (
                            table.shape[0] * table.shape[1]
                        )
                    )

        if file_name.endswith((".html", ".htm")):
            tables = pd.read_html(
                BytesIO(file_bytes),
                flavor="lxml",
                header=None,
            )

            if tables:
                return max(
                    tables,
                    key=lambda table: (
                        table.shape[0] * table.shape[1]
                    )
                )

        raise Exception(
            "Unsupported Petpooja report format. "
            "Please upload the original XLS, XLSX or HTML report."
        )

    except Exception as e:
        raise Exception(str(e)) from e
    
@st.cache_resource(show_spinner=False)
def get_finwise_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        try:
            api_key = st.secrets.get(
                "OPENAI_API_KEY"
            )
        except Exception:
            api_key = None

    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is missing."
        )

    return OpenAI(api_key=api_key)

def build_finwise_chat_context(
    phone,
    selected_year,
    base_month,
    comparison_month,
    monthly_df,
    category_comparison_df,
):
    """
    Builds a compact financial context for the chatbot.

    The chatbot receives only data belonging to the
    currently logged-in phone number.
    """

    phone_clean = clean_phone(phone)

    # ---------------------------------
    # Monthly totals for selected year
    # ---------------------------------
    monthly_records = []

    if monthly_df is not None and not monthly_df.empty:
        for _, row in monthly_df.iterrows():
            month_number = int(
                row.get("month_number", 0) or 0
            )

            if month_number < 1 or month_number > 12:
                continue

            monthly_records.append({
                "month_number": month_number,
                "month_name": calendar.month_name[
                    month_number
                ],
                "income": float(
                    row.get("total_income", 0) or 0
                ),
                "petpooja_income": float(
                    row.get("petpooja_income", 0) or 0
                ),
                "manual_income": float(
                    row.get("manual_income", 0) or 0
                ),
                "other_income": float(
                    row.get("entries_income", 0) or 0
                ),
                "expense": float(
                    row.get("expense", 0) or 0
                ),
                "net": float(
                    row.get("net", 0) or 0
                ),
                "expense_bill_count": int(
                    row.get("bill_count", 0) or 0
                ),
            })

    # ---------------------------------
    # Category comparison
    # ---------------------------------
    category_records = []

    if (
        category_comparison_df is not None
        and not category_comparison_df.empty
    ):
        for _, row in (
            category_comparison_df.iterrows()
        ):
            category_records.append({
                "category": str(
                    row.get(
                        "category",
                        "Uncategorized"
                    )
                ),
                "base_amount": float(
                    row.get("base_amount", 0) or 0
                ),
                "comparison_amount": float(
                    row.get(
                        "comparison_amount",
                        0
                    ) or 0
                ),
                "base_bill_count": int(
                    row.get(
                        "base_bill_count",
                        0
                    ) or 0
                ),
                "comparison_bill_count": int(
                    row.get(
                        "comparison_bill_count",
                        0
                    ) or 0
                ),
            })

    # ---------------------------------
    # Load the owner's expense records
    # ---------------------------------
    expenses_df = load_entries_for_user(
        phone_clean,
        limit=10000,
    )

    vendor_summary = []
    category_summary = []
    recent_expenses = []

    if not expenses_df.empty:
        expenses_df = expenses_df.copy()

        if "is_deleted" in expenses_df.columns:
            deleted_values = (
                expenses_df["is_deleted"]
                .fillna("no")
                .astype(str)
                .str.strip()
                .str.lower()
            )

            expenses_df = expenses_df[
                ~deleted_values.isin(
                    ["yes", "true", "1"]
                )
            ].copy()

        if "transaction_type" in expenses_df.columns:
            expenses_df = expenses_df[
                expenses_df[
                    "transaction_type"
                ]
                .fillna("expense")
                .astype(str)
                .str.strip()
                .str.lower()
                == "expense"
            ].copy()

        expenses_df["date_parsed"] = pd.to_datetime(
            expenses_df.get("date", ""),
            errors="coerce",
            dayfirst=False,
        )

        expenses_df["amount_numeric"] = (
            pd.to_numeric(
                expenses_df.get("total", 0),
                errors="coerce",
            ).fillna(0)
        )

        expenses_df["year"] = (
            expenses_df["date_parsed"].dt.year
        )

        selected_year_expenses = expenses_df[
            expenses_df["year"] == int(selected_year)
        ].copy()

        if not selected_year_expenses.empty:
            selected_year_expenses["vendor_clean"] = (
                selected_year_expenses.get(
                    "vendor",
                    pd.Series(
                        index=selected_year_expenses.index,
                        dtype=str,
                    )
                )
                .fillna("Unknown Vendor")
                .astype(str)
                .str.strip()
                .replace("", "Unknown Vendor")
            )

            selected_year_expenses[
                "category_clean"
            ] = (
                selected_year_expenses.get(
                    "category",
                    pd.Series(
                        index=selected_year_expenses.index,
                        dtype=str,
                    )
                )
                .fillna("Uncategorized")
                .astype(str)
                .str.strip()
                .replace("", "Uncategorized")
                .str.title()
            )

            vendor_df = (
                selected_year_expenses
                .groupby(
                    "vendor_clean",
                    as_index=False,
                )
                .agg(
                    amount=(
                        "amount_numeric",
                        "sum",
                    ),
                    bill_count=(
                        "amount_numeric",
                        "size",
                    ),
                )
                .sort_values(
                    "amount",
                    ascending=False,
                )
                .head(100)
            )

            vendor_summary = (
                vendor_df.to_dict("records")
            )

            category_df = (
                selected_year_expenses
                .groupby(
                    "category_clean",
                    as_index=False,
                )
                .agg(
                    amount=(
                        "amount_numeric",
                        "sum",
                    ),
                    bill_count=(
                        "amount_numeric",
                        "size",
                    ),
                )
                .sort_values(
                    "amount",
                    ascending=False,
                )
            )

            category_summary = (
                category_df.to_dict("records")
            )

            recent_df = (
                selected_year_expenses
                .sort_values(
                    "date_parsed",
                    ascending=False,
                )
                .head(100)
            )

            for _, row in recent_df.iterrows():
                recent_expenses.append({
                    "date": (
                        row["date_parsed"]
                        .strftime("%Y-%m-%d")
                        if pd.notna(
                            row["date_parsed"]
                        )
                        else str(
                            row.get("date", "")
                        )
                    ),
                    "vendor": str(
                        row.get("vendor", "")
                    ),
                    "category": str(
                        row.get("category", "")
                    ),
                    "description": str(
                        row.get("description", "")
                    ),
                    "amount": float(
                        row.get(
                            "amount_numeric",
                            0
                        ) or 0
                    ),
                })

    # ---------------------------------
    # Manual income
    # ---------------------------------
    manual_income_df = (
        load_income_entries_for_user(
            phone=phone_clean,
            limit=5000,
        )
    )

    manual_income_records = []

    if not manual_income_df.empty:
        for _, row in (
            manual_income_df.head(500).iterrows()
        ):
            manual_income_records.append({
                "date": str(
                    row.get("income_date", "")
                ),
                "customer": str(
                    row.get("customer_name", "")
                ),
                "event": str(
                    row.get("event_name", "")
                ),
                "category": str(
                    row.get(
                        "income_category",
                        ""
                    )
                ),
                "amount": float(
                    pd.to_numeric(
                        row.get("amount", 0),
                        errors="coerce",
                    )
                    or 0
                ),
                "payment_method": str(
                    row.get(
                        "payment_method",
                        ""
                    )
                ),
            })

    return {
        "account_phone": phone_clean,
        "currency_symbol": get_currency_symbol(phone_clean),
        "currency_code": get_currency_code(phone_clean),
        "selected_year": int(selected_year),
        "active_comparison": {
            "base_month_number": int(
                base_month
            ),
            "base_month_name": calendar.month_name[
                int(base_month)
            ],
            "comparison_month_number": int(
                comparison_month
            ),
            "comparison_month_name": (
                calendar.month_name[
                    int(comparison_month)
                ]
            ),
        },
        "monthly_summary": monthly_records,
        "selected_month_category_comparison": (
            category_records
        ),
        "selected_year_category_summary": (
            category_summary
        ),
        "selected_year_vendor_summary": (
            vendor_summary
        ),
        "recent_expenses": recent_expenses,
        "manual_income_records": (
            manual_income_records
        ),
        "data_notes": [
            (
                "Petpooja sales are included in "
                "monthly_summary.petpooja_income."
            ),
            (
                "A missing category may mean the "
                "expense was not uploaded."
            ),
            (
                "The available transaction lists may "
                "be limited to the configured database "
                "query limit."
            ),
        ],
    }

def answer_finwise_data_question(
    question,
    financial_context,
    conversation_history=None,
):
    client = get_finwise_openai_client()

    conversation_history = (
        conversation_history or []
    )

    recent_history = (
        conversation_history[-8:]
    )

    system_instructions = """
You are FinWise, a financial-data assistant for restaurant
owners and small-business owners.

You must answer using only the financial context supplied
by the FinWise application.

You can answer questions about:
- income;
- Petpooja sales;
- manual income;
- expenses;
- net income or loss;
- months and month-over-month comparisons;
- categories;
- vendors;
- bill counts;
- recent transactions;
- missing or newly appearing expenses;
- spending trends;
- possible business observations supported by the data.

Rules:
1. Never invent an amount, vendor, category, transaction,
   cause or date.
2. Clearly state when the supplied data is insufficient.
3. A missing expense may mean it was not uploaded. Do not
   state that it was definitely unpaid.
4. Use the currency_symbol and currency_code supplied in the
   financial context. Do not convert the stored amounts.
5. Explain results in clear business language.
6. Show the important calculations used in the answer.
7. When comparing negative net values, describe the monetary
   improvement or decline instead of using a misleading
   percentage.
8. Category names differing only by capitalization should
   be treated as the same category.
9. Answer the user's exact question first, then provide
   one useful observation when supported by the data.
10. Do not reveal phone numbers or internal database details.
11. If the question is unrelated to the supplied financial
    data, explain that this chatbot answers FinWise financial
    questions only.
"""

    request_payload = {
        "financial_context": financial_context,
        "recent_conversation": recent_history,
        "owner_question": question,
    }

    response = client.responses.create(
        model="gpt-4.1-mini",
        instructions=system_instructions,
        input=json.dumps(
            request_payload,
            default=str,
            ensure_ascii=False,
        ),
    )

    answer = str(
        response.output_text or ""
    ).strip()

    if not answer:
        return (
            "I could not generate an answer from the "
            "available FinWise data."
        )

    return answer

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


def metric_card(label, value, icon, bg, color, phone_number):
    st.markdown(
        f'''
        <div style="background:white; border:1px solid #E2E8F0; border-radius:18px; padding:22px; display:flex; gap:18px; align-items:center; box-shadow:0 8px 24px rgba(15,23,42,0.05);">
            <div style="background:{bg}; color:{color}; width:62px; height:62px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:30px; font-weight:900;">{icon}</div>
            <div>
                <div style="color:#64748B; font-size:14px; font-weight:700;">{label}</div>
                <div style="color:{color}; font-size:30px; font-weight:900;">{format_currency(value, phone_number)}</div>
            </div>
        </div>
        ''',
        unsafe_allow_html=True,
    )

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv(
    "TWILIO_WHATSAPP_FROM",
    "whatsapp:+14155238886"
)

def calculate_percentage_change(
    base_value,
    comparison_value
):
    base_value = float(base_value or 0)
    comparison_value = float(
        comparison_value or 0
    )

    if base_value == 0:
        if comparison_value == 0:
            return 0.0

        return None

    return (
        (comparison_value - base_value)
        / abs(base_value)
    ) * 100


def format_change_message(
    label,
    base_value,
    comparison_value,
    base_month_name,
    comparison_month_name,
    currency=True,
    phone_number="",
):
    base_value = float(base_value or 0)
    comparison_value = float(
        comparison_value or 0
    )

    difference = comparison_value - base_value

    percentage = calculate_percentage_change(
        base_value,
        comparison_value
    )

    amount_prefix = get_currency_symbol(phone_number) if currency else ""

    if base_value == 0 and comparison_value > 0:
        return (
            f"**{label}: New in {comparison_month_name}.** "
            f"{comparison_month_name}: "
            f"{amount_prefix}{comparison_value:,.2f}."
        )

    if base_value > 0 and comparison_value == 0:
        return (
            f"**{label}: No record in "
            f"{comparison_month_name}.** "
            f"{base_month_name}: "
            f"{amount_prefix}{base_value:,.2f}."
        )

    if percentage is None:
        return (
            f"**{label}:** "
            f"{comparison_month_name}: "
            f"{amount_prefix}{comparison_value:,.2f}."
        )

    if percentage > 0:
        direction = "increased"
    elif percentage < 0:
        direction = "decreased"
    else:
        direction = "did not change"

    if percentage == 0:
        return (
            f"**{label} did not change.** "
            f"Both months: "
            f"{amount_prefix}{comparison_value:,.2f}."
        )

    return (
        f"**{label} {direction} by "
        f"{abs(percentage):,.1f}%.** "
        f"{base_month_name}: "
        f"{amount_prefix}{base_value:,.2f} → "
        f"{comparison_month_name}: "
        f"{amount_prefix}{comparison_value:,.2f} "
        f"({amount_prefix}{difference:+,.2f})."
    )


def month_has_data(row):
    return any([
        float(row.get("total_income", 0) or 0) != 0,
        float(row.get("expense", 0) or 0) != 0,
        int(row.get("bill_count", 0) or 0) != 0,
    ])

def prepare_expense_pie_data(
    category_comparison_df,
    amount_column,
    max_categories=7,
):
    """
    Prepares category expense data for one monthly pie chart.

    - Combines category names that differ only by capitalization.
    - Keeps the largest categories.
    - Groups remaining categories into Other.
    """

    if (
        category_comparison_df is None
        or category_comparison_df.empty
        or amount_column not in category_comparison_df.columns
    ):
        return pd.DataFrame(
            columns=["category", "amount"]
        )

    pie_df = category_comparison_df[
        ["category", amount_column]
    ].copy()

    pie_df["category"] = (
        pie_df["category"]
        .fillna("Uncategorized")
        .astype(str)
        .str.strip()
        .replace("", "Uncategorized")
        .str.lower()
        .str.title()
    )

    pie_df["amount"] = pd.to_numeric(
        pie_df[amount_column],
        errors="coerce",
    ).fillna(0)

    pie_df = pie_df[
        pie_df["amount"] > 0
    ].copy()

    if pie_df.empty:
        return pd.DataFrame(
            columns=["category", "amount"]
        )

    # Combine duplicates such as Mutton and mutton.
    pie_df = (
        pie_df.groupby(
            "category",
            as_index=False,
        )["amount"]
        .sum()
        .sort_values(
            "amount",
            ascending=False,
        )
        .reset_index(drop=True)
    )

    if len(pie_df) > max_categories:
        top_df = pie_df.head(
            max_categories
        ).copy()

        other_amount = float(
            pie_df.iloc[
                max_categories:
            ]["amount"].sum()
        )

        if other_amount > 0:
            other_row = pd.DataFrame([
                {
                    "category": "Other",
                    "amount": other_amount,
                }
            ])

            pie_df = pd.concat(
                [top_df, other_row],
                ignore_index=True,
            )
        else:
            pie_df = top_df

    return pie_df

def create_expense_pie_chart(
    pie_df,
    month_name,
    year,
    phone_number,
):
    """
    Creates one category expense donut chart.
    """

    fig, ax = plt.subplots(
        figsize=(7, 6)
    )

    if pie_df.empty:
        ax.text(
            0.5,
            0.5,
            "No expense data",
            horizontalalignment="center",
            verticalalignment="center",
            fontsize=15,
        )

        ax.axis("off")

        ax.set_title(
            f"{month_name} {year}",
            fontsize=16,
            fontweight="bold",
        )

        return fig

    amounts = pie_df["amount"].tolist()
    categories = pie_df["category"].tolist()

    total_amount = float(
        pie_df["amount"].sum()
    )

    def show_percentage(percentage):
        # Avoid clutter from very small slices.
        if percentage < 3:
            return ""

        return f"{percentage:.1f}%"

    wedges, _, _ = ax.pie(
        amounts,
        labels=None,
        autopct=show_percentage,
        startangle=90,
        pctdistance=0.78,
        wedgeprops={
            "width": 0.45,
            "edgecolor": "white",
        },
        textprops={
            "fontsize": 10,
        },
    )

    ax.text(
        0,
        0.08,
        f"{month_name}",
        horizontalalignment="center",
        verticalalignment="center",
        fontsize=14,
        fontweight="bold",
    )

    ax.text(
        0,
        -0.10,
        format_currency(total_amount, phone_number, decimals=0),
        horizontalalignment="center",
        verticalalignment="center",
        fontsize=13,
    )

    legend_labels = [
        f"{category}: {format_currency(amount, phone_number, decimals=0)}"
        for category, amount in zip(
            categories,
            amounts,
        )
    ]

    ax.legend(
        wedges,
        legend_labels,
        title="Expense Categories",
        loc="center left",
        bbox_to_anchor=(1, 0.5),
        fontsize=9,
    )

    ax.set_title(
        f"{month_name} {year}",
        fontsize=16,
        fontweight="bold",
        pad=18,
    )

    ax.axis("equal")
    fig.tight_layout()

    return fig

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
        auth_mode = st.radio(
            "Choose option",
            ["Login", "Register", "Forgot Password"],
            horizontal=True,
            label_visibility="collapsed",
        )

        if auth_mode == "Login":
            with st.form("finwise_login_form", clear_on_submit=False):
                phone_input = st.text_input(
                    "WhatsApp phone number",
                    placeholder="+91 98765 43210",
                    autocomplete="tel",
                    key="login_phone_input",
                )

                password_input = st.text_input(
                    "Password",
                    type="password",
                    placeholder="Enter password",
                    autocomplete="current-password",
                    key="login_password_input",
                )

                login_submitted = st.form_submit_button(
                    "Login",
                    type="primary",
                    use_container_width=True,
                )

            render_autofill_sync()

            if login_submitted:
                phone_value = str(phone_input or "").strip()
                password_value = str(password_input or "").strip()

                if not phone_value:
                    st.warning(
                        "Please select or type your saved WhatsApp "
                        "number, then click Login again."
                    )

                elif not password_value:
                    st.warning("Please enter your password.")

                elif validate_login(phone_value, password_value):
                    st.session_state.logged_in = True
                    st.session_state.user_phone = clean_phone(phone_value)

                    st.query_params["logged_in"] = "true"
                    st.query_params["phone"] = clean_phone(phone_value)

                    st.rerun()

                else:
                    st.error("Invalid phone number or password.")

        elif auth_mode == "Register":
            with st.form("finwise_register_form", clear_on_submit=False):
                phone_input = st.text_input(
                    "WhatsApp phone number",
                    placeholder="+91 98765 43210",
                    autocomplete="tel",
                    key="register_phone_input",
                )

                password_input = st.text_input(
                    "Password",
                    type="password",
                    placeholder="Enter password",
                    autocomplete="new-password",
                    key="register_password_input",
                )

                register_submitted = st.form_submit_button(
                    "Create Account",
                    type="primary",
                    use_container_width=True,
                )

            if register_submitted:
                phone_value = str(phone_input or "").strip()
                password_value = str(password_input or "").strip()

                if not phone_value or not password_value:
                    st.error("Enter phone number and password.")
                else:
                    success, message = register_user(
                        phone_value,
                        password_value,
                    )

                    if success:
                        st.success(message)
                    else:
                        st.error(message)

        else:
            phone_input = st.text_input(
                "WhatsApp phone number",
                placeholder="+91 98765 43210",
                autocomplete="tel",
                key="reset_phone_input",
            )

            st.info(
                "We will send a 6-digit reset code to your "
                "WhatsApp number."
            )

            if st.button(
                "Send WhatsApp Code",
                type="primary",
                use_container_width=True,
            ):
                if not phone_input:
                    st.error("Enter your WhatsApp phone number.")
                else:
                    try:
                        send_password_reset_code(phone_input)
                        st.success("Reset code sent to your WhatsApp.")
                    except Exception as e:
                        st.error(
                            "Could not send WhatsApp code. "
                            f"Error: {str(e)}"
                        )

            reset_code_input = st.text_input(
                "Enter WhatsApp Code",
                placeholder="Enter 6-digit code"
            )

            new_password = st.text_input(
                "New Password",
                type="password",
                placeholder="Enter new password",
                autocomplete="new-password",
            )

            if st.button("Reset Password", use_container_width=True):
                if not phone_input or not reset_code_input or not new_password:
                    st.error(
                        "Enter phone number, WhatsApp code, "
                        "and new password."
                    )

                elif "reset_code" not in st.session_state:
                    st.error("Please request a WhatsApp code first.")

                elif time.time() > st.session_state.get(
                    "reset_code_expiry",
                    0,
                ):
                    st.error(
                        "Reset code expired. Please request a new code."
                    )

                elif clean_phone(phone_input) != st.session_state.get(
                    "reset_phone",
                    "",
                ):
                    st.error(
                        "Phone number does not match the reset code."
                    )

                elif reset_code_input.strip() != st.session_state.get(
                    "reset_code",
                    "",
                ):
                    st.error("Invalid WhatsApp code.")

                else:
                    success, message = reset_password(
                        phone_input,
                        new_password,
                    )

                    if success:
                        st.success(
                            "Password reset successfully. Please login."
                        )

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
            "📈 Month-over-Month Analysis",
            "⚙️ Settings",
            "🗑️ Recently Deleted",
            "💡 Help Shape FinWise",
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

    # Load enough records first so newly inserted database IDs
    # are not excluded because of an older bill date.
    df = cached_load_entries_for_user(
        phone,
        limit=st.session_state.expense_limit,
    )
    if not df.empty and "is_deleted" in df.columns:
        deleted_values = (
            df["is_deleted"]
            .fillna("no")
            .astype(str)
            .str.strip()
            .str.lower()
        )

        df = df[
            ~deleted_values.isin(["yes", "true", "1"])
        ].copy()

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
        if st.button(
            "Load 100 more bills",
            use_container_width=True,
        ):
            st.session_state.expense_limit += 100
            st.rerun()

    with col_all:
        if st.button(
            "Show up to 1,000 bills",
            use_container_width=True,
        ):
            st.session_state.expense_limit = 1000
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

    manual_income_total = cached_manual_income_total(
        phone,
        sql_start_date,
        sql_end_date
    )

    total_income = (
        whatsapp_income
        + petpooja_filtered_income
        + manual_income_total
    )

    net_amount = total_income - total_expense

    m1, m2, m3 = st.columns(3)
    with m1:
        metric_card("Total Income", total_income, "↗", "#DCFCE7", "#16A34A", phone)
    with m2:
        metric_card("Total Expenses", total_expense, "↘", "#FEE2E2", "#DC2626", phone)
    with m3:
        metric_card("Net", net_amount, "💼", "#DBEAFE", "#2563EB", phone)

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
            manual_type = "expense"

            st.text_input(
                "Type",
                value="Expense",
                disabled=True,
                key="manual_expense_type_display"
            )
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
                    "category": normalize_category(manual_category),
                    "folder": normalize_category(manual_category),
                    "subtotal": float(manual_amount),
                    "tax": 0,
                    "total": float(manual_amount),
                    "currency": get_currency_code(phone),
                    "confidence": "manual",
                    "reason": "Manual dashboard entry",
                    "image_path": "",
                    "source": "Manual Dashboard",
                }

                append_entry(manual_entry)

                update_vendor_memory(
                    user_phone=phone,
                    vendor=manual_vendor.strip().title(),
                    category=normalize_category(manual_category),
                    folder=normalize_category(manual_category),
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
        display_df["Expense Number"] = (
            "EXP-" + original_df["id"].astype(str)
        )

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
                        format=get_currency_column_format(phone),
                    ),
                    "Delete?": st.column_config.CheckboxColumn(
                        "Delete?",
                        help="Select this to delete the expense",
                        default=False,
                    ),
                },
                disabled=["Expense Number", "Type"],
            )

        with st.container(key="mobile_expense_editor"):
            st.info("For full table view, please log in from desktop.")
            st.markdown("#### Mobile Edit View")

            mobile_rows = []

            mobile_display_df = display_df.head(25)

            for i, row in mobile_display_df.iterrows():
                with st.expander(
                    f'{row["Expense Number"]} • {row["Date"]} • {row["Vendor"]} • {format_currency(row["Amount"], phone)}',
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

            original_lookup = original_df.set_index(original_df["id"].astype(str))

            for _, row in edited_data.iterrows():
                entry_id = str(row.get("db_id", "")).strip()

                if not entry_id or entry_id not in original_lookup.index:
                    continue

                original_row = original_lookup.loc[entry_id]

                new_category = normalize_category(row.get("Category", "")
)
                new_vendor = str(row.get("Vendor", "")).strip()
                new_description = str(row.get("Description", "")).strip()
                new_amount = float(pd.to_numeric(row.get("Amount", 0), errors="coerce") or 0)

                old_category = str(original_row.get("category", "")).strip()
                old_vendor = str(original_row.get("vendor", "")).strip()
                old_description = str(original_row.get("description", "")).strip()
                old_amount = float(pd.to_numeric(original_row.get("total", 0), errors="coerce") or 0)

                category_changed = new_category != old_category
                vendor_changed = new_vendor != old_vendor
                description_changed = new_description != old_description
                amount_changed = new_amount != old_amount

                if not any([category_changed, vendor_changed, description_changed, amount_changed]):
                    continue

                update_entry_by_id(
                    entry_id=entry_id,
                    category=new_category if category_changed else None,
                    amount=new_amount if amount_changed else None,
                    vendor=new_vendor if vendor_changed else None,
                    description=new_description if description_changed else None,
                )

                updated_count += 1

                # Only update vendor memory when vendor/category changed
                if category_changed or vendor_changed:
                    if new_vendor and new_category:
                        update_vendor_memory(
                            user_phone=phone,
                            vendor=new_vendor,
                            category=new_category,
                            folder=new_category,
                        )

            return updated_count

        def delete_selected_expenses(edited_data, original_df, phone):
            if edited_data.empty or "Delete?" not in edited_data.columns:
                return 0

            selected_rows = edited_data[
                edited_data["Delete?"] == True
            ]

            if selected_rows.empty:
                return 0

            ids_to_delete = (
                selected_rows["db_id"]
                .astype(str)
                .str.strip()
                .tolist()
            )

            return soft_delete_entries_by_ids(
                entry_ids=ids_to_delete,
                owner_phone=phone,
                deleted_by=phone,
                delete_source="streamlit",
            )
        
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
                    st.success(f"Moved {deleted_count} record(s) to Recently Deleted.")
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
                    st.success(
                        f"Moved {deleted_count} record(s) to Recently Deleted."
                    )
                    st.cache_data.clear()
                    st.rerun()

    st.write("### Petpooja Sales Summary")
    income_metric_1, income_metric_2 = st.columns(2)

    with income_metric_1:
        st.metric(
            "Petpooja Total Sales",
            format_currency(petpooja_filtered_income, phone)
        )

    with income_metric_2:
        st.metric(
            "Manual Event / Catering Income",
            format_currency(manual_income_total, phone)
        )


    # Upload, preview and then save Petpooja report
    st.write("### Upload Petpooja Daily/Monthly Sales Summary")

    uploaded_sales_file = st.file_uploader(
        "Upload Petpooja Daily/Monthly Sales Summary",
        type=["xls", "xlsx", "html"],
        key="petpooja_sales_upload",
    )

    if uploaded_sales_file is not None:
        st.caption(
            f"Selected file: {uploaded_sales_file.name} "
            f"({uploaded_sales_file.size / 1024:.1f} KB)"
        )

        if st.button(
            "Preview Petpooja Report",
            type="primary",
            key="preview_petpooja_report_button",
        ):
            try:
                with st.spinner("Reading Petpooja report..."):
                    raw_sales_df = read_petpooja_file(
                        uploaded_sales_file
                    )

                    preview_df = build_petpooja_report_df(
                        raw_sales_df
                    )

                if preview_df.empty:
                    st.warning(
                        "Could not find Petpooja order rows "
                        "in this report."
                    )

                else:
                    preview_df["user_phone"] = clean_phone(
                        phone
                    )

                    preview_report_id = uuid.uuid4().hex

                    preview_df["report_id"] = (
                        preview_report_id
                    )

                    preview_df["source_filename"] = (
                        uploaded_sales_file.name
                    )

                    preview_df["duplicate_key"] = (
                        preview_df.apply(
                            lambda row: make_petpooja_duplicate_key(
                                row,
                                phone,
                            ),
                            axis=1,
                        )
                    )

                    st.session_state[
                        "petpooja_upload_preview"
                    ] = preview_df

                    st.session_state[
                        "petpooja_upload_filename"
                    ] = uploaded_sales_file.name

                    st.success(
                        f"Report read successfully. "
                        f"{len(preview_df):,} rows found."
                    )

            except Exception as e:
                st.error(
                    f"Could not read Petpooja file: {e}"
                )

    petpooja_upload_preview = st.session_state.get(
    "petpooja_upload_preview"
    )

    if petpooja_upload_preview is not None:
        st.write("### Petpooja Report Preview")

        preview_columns = [
            "Order No.",
            "Date",
            "Payment Type",
            "Order Type",
            "My Amount",
            "Discount",
            "Total",
            "Biller Name",
        ]

        available_preview_columns = [
            column
            for column in preview_columns
            if column in petpooja_upload_preview.columns
        ]

        if available_preview_columns:
            st.dataframe(
                petpooja_upload_preview[
                    available_preview_columns
                ].head(100),
                width="stretch",
                hide_index=True,
            )
        else:
            st.warning(
                "The report was read, but preview columns "
                "could not be identified."
            )

        preview_total = float(
            pd.to_numeric(
                petpooja_upload_preview[
                    "petpooja_total"
                ],
                errors="coerce",
            ).fillna(0).sum()
        )

        preview_col1, preview_col2 = st.columns(2)

        with preview_col1:
            st.metric(
                "Orders detected",
                f"{len(petpooja_upload_preview):,}"
            )

        with preview_col2:
            st.metric(
                "Report total",
                format_currency(preview_total, phone)
            )

        save_col, cancel_col = st.columns(2)

        with save_col:
            if st.button(
                "Save Petpooja Report",
                type="primary",
                width="stretch",
                key="save_petpooja_report_button",
            ):
                try:
                    with st.spinner(
                        "Saving Petpooja report..."
                    ):
                        existing_petpooja_df = (
                            load_petpooja_entries_for_user(
                                phone,
                                limit=10000,
                            )
                        )

                        if (
                            not existing_petpooja_df.empty
                            and "duplicate_key"
                            in existing_petpooja_df.columns
                        ):
                            existing_keys = set(
                                existing_petpooja_df[
                                    "duplicate_key"
                                ].astype(str)
                            )
                        else:
                            existing_keys = set()

                        new_petpooja_df = (
                            petpooja_upload_preview[
                                ~petpooja_upload_preview[
                                    "duplicate_key"
                                ].astype(str).isin(
                                    existing_keys
                                )
                            ].copy()
                        )

                        duplicate_count = (
                            len(petpooja_upload_preview)
                            - len(new_petpooja_df)
                        )

                        inserted_count = 0

                        if not new_petpooja_df.empty:
                            inserted_count = (
                                append_petpooja_report(
                                    new_petpooja_df
                                )
                            )

                    st.session_state.pop(
                        "petpooja_upload_preview",
                        None,
                    )

                    st.session_state.pop(
                        "petpooja_upload_filename",
                        None,
                    )

                    st.cache_data.clear()

                    st.success(
                        f"Report saved. Added "
                        f"{inserted_count:,} records. "
                        f"Skipped {duplicate_count:,} "
                        f"duplicates."
                    )

                    st.rerun()

                except Exception as e:
                    st.error(
                        f"Could not save Petpooja report: {e}"
                    )

        with cancel_col:
            if st.button(
                "Cancel Preview",
                width="stretch",
                key="cancel_petpooja_preview_button",
            ):
                st.session_state.pop(
                    "petpooja_upload_preview",
                    None,
                )

                st.session_state.pop(
                    "petpooja_upload_filename",
                    None,
                )

                st.rerun()          
    

    if not petpooja_saved_df.empty:
        payment_summary = (
            petpooja_saved_df.groupby("payment_method")["petpooja_total"]
            .sum()
            .reset_index()
            .sort_values("petpooja_total", ascending=False)
        )
        st.write("### Petpooja Payment Summary")
        payment_summary = payment_summary.reset_index(drop=True)

        payment_summary_display = payment_summary.rename(
            columns={
                "payment_method": "Payment Method",
                "petpooja_total": "Sales Total",
            }
        )

        st.dataframe(
            payment_summary_display,
            width="stretch",
            hide_index=True,
            column_config={
                "Sales Total": st.column_config.NumberColumn(
                    "Sales Total",
                    format=get_currency_column_format(phone),
                ),
            },
        )

        payment_summary_mobile = payment_summary_display.copy()
        payment_summary_mobile["Sales Total"] = (
            payment_summary_mobile["Sales Total"].apply(
                lambda amount: format_currency(amount, phone)
            )
        )

        st.markdown(
            f'<div class="mobile-table">'
            f'{mobile_table_html(payment_summary_mobile)}'
            f'</div>',
            unsafe_allow_html=True,
        )

        st.write("### Uploaded Petpooja Reports")

        petpooja_reports_df = load_petpooja_reports_for_user(
            phone
        )

        if petpooja_reports_df.empty:
            st.info(
                "No individually tracked Petpooja reports "
                "are available yet."
            )

        else:
            for _, report_row in petpooja_reports_df.iterrows():
                report_id = str(
                    report_row.get("report_id", "")
                )

                source_filename = str(
                    report_row.get(
                        "source_filename",
                        "Petpooja Report"
                    )
                )

                row_count = int(
                    report_row.get("row_count", 0) or 0
                )

                report_total = float(
                    report_row.get("report_total", 0) or 0
                )

                start_date_value = report_row.get(
                    "start_date"
                )

                end_date_value = report_row.get(
                    "end_date"
                )

                uploaded_at = report_row.get(
                    "uploaded_at"
                )

                date_range_text = "Date unavailable"

                if (
                    pd.notna(start_date_value)
                    and pd.notna(end_date_value)
                ):
                    date_range_text = (
                        f"{pd.to_datetime(start_date_value).strftime('%d-%b-%Y')}"
                        f" to "
                        f"{pd.to_datetime(end_date_value).strftime('%d-%b-%Y')}"
                    )

                expander_title = (
                    f"📄 {source_filename} • "
                    f"{row_count:,} rows • "
                    + format_currency(report_total, phone)
                )

                with st.expander(
                    expander_title,
                    expanded=False
                ):
                    st.write(
                        f"**Sales period:** {date_range_text}"
                    )

                    if pd.notna(uploaded_at):
                        st.write(
                            "**Uploaded:** "
                            f"{pd.to_datetime(uploaded_at).strftime('%d-%b-%Y %I:%M %p')}"
                        )

                    report_records_df = (
                        petpooja_saved_df[
                            petpooja_saved_df[
                                "report_id"
                            ].astype(str) == report_id
                        ].copy()
                    )

                    preview_columns = [
                        "order_no",
                        "date",
                        "payment_type",
                        "order_type",
                        "my_amount",
                        "discount",
                        "total",
                        "biller_name",
                    ]

                    available_columns = [
                        column
                        for column in preview_columns
                        if column in report_records_df.columns
                    ]

                    if available_columns:
                        st.dataframe(
                            report_records_df[
                                available_columns
                            ].head(500),
                            width="stretch",
                            hide_index=True,
                        )

                    confirm_delete = st.checkbox(
                        "I understand this will remove this "
                        "report and update all totals.",
                        key=f"confirm_petpooja_delete_{report_id}",
                    )

                    if st.button(
                        "🗑️ Delete This Report",
                        key=f"delete_petpooja_report_{report_id}",
                        disabled=not confirm_delete,
                    ):
                        deleted_count = (
                            delete_petpooja_report(
                                report_id=report_id,
                                phone=phone,
                            )
                        )

                        if deleted_count:
                            st.success(
                                f"Deleted {deleted_count} Petpooja "
                                "records from this report."
                            )

                            st.cache_data.clear()
                            st.rerun()

                        else:
                            st.warning(
                                "The report could not be deleted."
                            )

            

    st.write("### 💰 Manual Income")

    st.caption(
        "Add income that is not included in Petpooja, "
        "such as catering, party events or private orders."
    )

    with st.expander(
        "➕ Add Manual Income",
        expanded=False
    ):
        income_col1, income_col2, income_col3 = st.columns(3)

        with income_col1:
            income_date = st.date_input(
                "Income Date",
                value=date.today(),
                key="manual_income_date"
            )

            income_customer = st.text_input(
                "Customer Name",
                placeholder="Example: Uma",
                key="manual_income_customer"
            )

        with income_col2:
            income_event_name = st.text_input(
                "Event / Order Name",
                placeholder="Example: Birthday Catering",
                key="manual_income_event"
            )

            income_category = st.selectbox(
                "Income Category",
                [
                    "Catering Income",
                    "Party Event Income",
                    "Private Event Income",
                    "Corporate Event Income",
                    "Bulk Order Income",
                    "Other Income",
                ],
                key="manual_income_category"
            )

        with income_col3:
            income_amount = st.number_input(
                "Income Amount",
                min_value=0.0,
                step=100.0,
                format="%.2f",
                key="manual_income_amount"
            )

            income_payment_method = st.selectbox(
                "Payment Method",
                [
                    "Cash",
                    "UPI",
                    "Bank Transfer",
                    "Credit Card",
                    "Cheque",
                    "Other",
                ],
                key="manual_income_payment_method"
            )

        income_description = st.text_area(
            "Description",
            placeholder=(
                "Example: Catering for 80 guests; "
                "full payment received"
            ),
            key="manual_income_description"
        )

        if st.button(
            "💾 Save Income",
            type="primary",
            use_container_width=True,
            key="save_manual_income"
        ):
            customer_value = income_customer.strip()
            event_value = income_event_name.strip()

            if not customer_value:
                st.error("Please enter the customer name.")

            elif not event_value:
                st.error("Please enter the event or order name.")

            elif income_amount <= 0:
                st.error(
                    "Please enter an income amount greater than 0."
                )

            else:
                income_entry = {
                    "user_phone": clean_phone(phone),
                    "income_date": normalize_date_ddmmyyyy(
                        str(income_date)
                    ),
                    "customer_name": customer_value.title(),
                    "event_name": event_value.title(),
                    "income_category": income_category,
                    "description": (
                        income_description.strip()
                        or event_value
                    ),
                    "amount": float(income_amount),
                    "payment_method": income_payment_method,
                    "currency": get_currency_code(phone),
                    "source": "Manual Dashboard",
                }

                income_id = append_income_entry(
                    income_entry
                )

                st.success(
                    f"Income saved successfully. "
                    f"Reference: INC-{income_id}"
                )

                st.cache_data.clear()
                st.rerun()

    if "show_income_table" not in st.session_state:
        st.session_state.show_income_table = False


    income_button_label = (
        "Hide Income Records"
        if st.session_state.show_income_table
        else "View Income Records"
    )

    if st.button(
        income_button_label,
        use_container_width=True,
        key="toggle_income_table"
    ):
        st.session_state.show_income_table = (
            not st.session_state.show_income_table
        )


    if st.session_state.show_income_table:
        income_df = cached_income_entries(
            phone=phone,
            start_date=sql_start_date,
            end_date=sql_end_date,
            limit=500,
        )

        if income_df.empty:
            st.info(
                "No manual income records found for "
                "the selected timeframe."
            )

        else:
            original_income_df = (
                income_df
                .reset_index(drop=True)
                .copy()
            )

            income_editor_df = pd.DataFrame()

            income_editor_df["db_id"] = (
                original_income_df["id"]
                .astype(str)
            )

            income_editor_df["Reference"] = (
                "INC-"
                + original_income_df["id"].astype(str)
            )

            income_editor_df["Date"] = (
                original_income_df["income_date"]
                .astype(str)
            )

            income_editor_df["Customer"] = (
                original_income_df["customer_name"]
                .fillna("")
                .astype(str)
            )

            income_editor_df["Event / Order"] = (
                original_income_df["event_name"]
                .fillna("")
                .astype(str)
            )

            income_editor_df["Category"] = (
                original_income_df["income_category"]
                .fillna("Other Income")
                .astype(str)
            )

            income_editor_df["Description"] = (
                original_income_df["description"]
                .fillna("")
                .astype(str)
            )

            income_editor_df["Amount"] = pd.to_numeric(
                original_income_df["amount"],
                errors="coerce"
            ).fillna(0)

            income_editor_df["Payment Method"] = (
                original_income_df["payment_method"]
                .fillna("")
                .astype(str)
            )

            income_editor_df["Delete?"] = False

        edited_income_df = st.data_editor(
            income_editor_df,
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            key="manual_income_editor",
            column_config={
                "Date": st.column_config.TextColumn(
                    "Date",
                    help="Use YYYY-MM-DD"
                ),
                "Category": st.column_config.SelectboxColumn(
                    "Category",
                    options=[
                        "Catering Income",
                        "Party Event Income",
                        "Private Event Income",
                        "Corporate Event Income",
                        "Bulk Order Income",
                        "Other Income",
                    ],
                    required=True,
                ),
                "Amount": st.column_config.NumberColumn(
                    "Amount",
                    min_value=0.0,
                    step=100.0,
                    format=get_currency_column_format(phone),
                ),
                "Payment Method": (
                    st.column_config.SelectboxColumn(
                        "Payment Method",
                        options=[
                            "Cash",
                            "UPI",
                            "Bank Transfer",
                            "Credit Card",
                            "Cheque",
                            "Other",
                        ],
                    )
                ),
                "Delete?": st.column_config.CheckboxColumn(
                    "Delete?",
                    default=False,
                ),
            },
            disabled=[
                "db_id",
                "Reference",
            ],
        )

        income_save_col, income_delete_col = (
            st.columns(2)
        )

        with income_save_col:
            if st.button(
                "💾 Save Income Changes",
                type="primary",
                use_container_width=True,
                key="save_income_changes",
            ):
                updated_count = 0

                original_lookup = (
                    original_income_df
                    .set_index(
                        original_income_df[
                            "id"
                        ].astype(str)
                    )
                )

                for _, edited_row in (
                    edited_income_df.iterrows()
                ):
                    entry_id = str(
                        edited_row.get(
                            "db_id",
                            ""
                        )
                    ).strip()

                    if (
                        not entry_id
                        or entry_id
                        not in original_lookup.index
                    ):
                        continue

                    original_row = (
                        original_lookup.loc[
                            entry_id
                        ]
                    )

                    new_date = str(
                        edited_row.get(
                            "Date",
                            ""
                        )
                    ).strip()

                    new_customer = str(
                        edited_row.get(
                            "Customer",
                            ""
                        )
                    ).strip()

                    new_event = str(
                        edited_row.get(
                            "Event / Order",
                            ""
                        )
                    ).strip()

                    new_category = str(
                        edited_row.get(
                            "Category",
                            ""
                        )
                    ).strip()

                    new_description = str(
                        edited_row.get(
                            "Description",
                            ""
                        )
                    ).strip()

                    new_amount = float(
                        pd.to_numeric(
                            edited_row.get(
                                "Amount",
                                0
                            ),
                            errors="coerce",
                        )
                        or 0
                    )

                    new_payment_method = str(
                        edited_row.get(
                            "Payment Method",
                            ""
                        )
                    ).strip()

                    old_date = str(
                        original_row.get(
                            "income_date",
                            ""
                        )
                    ).strip()

                    old_customer = str(
                        original_row.get(
                            "customer_name",
                            ""
                        )
                    ).strip()

                    old_event = str(
                        original_row.get(
                            "event_name",
                            ""
                        )
                    ).strip()

                    old_category = str(
                        original_row.get(
                            "income_category",
                            ""
                        )
                    ).strip()

                    old_description = str(
                        original_row.get(
                            "description",
                            ""
                        )
                    ).strip()

                    old_amount = float(
                        pd.to_numeric(
                            original_row.get(
                                "amount",
                                0
                            ),
                            errors="coerce",
                        )
                        or 0
                    )

                    old_payment_method = str(
                        original_row.get(
                            "payment_method",
                            ""
                        )
                    ).strip()

                    changes = {
                        "income_date": (
                            new_date
                            if new_date != old_date
                            else None
                        ),
                        "customer_name": (
                            new_customer
                            if new_customer
                            != old_customer
                            else None
                        ),
                        "event_name": (
                            new_event
                            if new_event != old_event
                            else None
                        ),
                        "income_category": (
                            new_category
                            if new_category
                            != old_category
                            else None
                        ),
                        "description": (
                            new_description
                            if new_description
                            != old_description
                            else None
                        ),
                        "amount": (
                            new_amount
                            if new_amount != old_amount
                            else None
                        ),
                        "payment_method": (
                            new_payment_method
                            if new_payment_method
                            != old_payment_method
                            else None
                        ),
                    }

                    if not any(
                        value is not None
                        for value in changes.values()
                    ):
                        continue

                    updated_count += (
                        update_income_entry_by_id(
                            entry_id=entry_id,
                            user_phone=phone,
                            **changes,
                        )
                    )

                if updated_count:
                    st.success(
                        f"Updated {updated_count} "
                        f"income record(s)."
                    )
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.info(
                        "No income changes were found."
                    )

        with income_delete_col:
            if st.button(
                "🗑️ Delete Selected Income",
                use_container_width=True,
                key="delete_selected_income",
            ):
                selected_income = (
                    edited_income_df[
                        edited_income_df[
                            "Delete?"
                        ] == True
                    ]
                )

                if selected_income.empty:
                    st.warning(
                        "Select at least one income "
                        "record to delete."
                    )

                else:
                    deleted_count = 0

                    for entry_id in (
                        selected_income[
                            "db_id"
                        ]
                        .astype(str)
                        .tolist()
                    ):
                        deleted_count += (
                            soft_delete_income_entry(
                                entry_id=entry_id,
                                user_phone=phone,
                            )
                        )

                    if deleted_count:
                        st.success(
                            f"Deleted {deleted_count} "
                            f"income record(s)."
                        )
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.warning(
                            "No income records were deleted."
                        )

    st.write("### Download Report")

    if st.button(
        "📄 Prepare Excel Download",
        use_container_width=True,
        key="prepare_excel_download"
    ):
        output = BytesIO()

        # -----------------------------
        # Expense export
        # -----------------------------
        expense_export_df = df.copy()

        if not expense_export_df.empty:
            expense_export_df = expense_export_df.reset_index(drop=True)

            if "id" in expense_export_df.columns:
                expense_export_df.insert(
                    0,
                    "Expense Reference",
                    "EXP-" + expense_export_df["id"].astype(str)
                )

            expense_export_df = expense_export_df.drop(
                columns=[
                    "user_phone_clean",
                    "id",
                    "date_parsed",
                    "user_phone",
                    "subtotal",
                    "tax",
                    "currency",
                    "confidence",
                    "reason",
                    "image_path",
                    "created_at",
                    "duplicate_key",
                    "is_deleted",
                    "deleted_at",
                    "deleted_by",
                    "delete_source",
                ],
                errors="ignore"
            )

        # -----------------------------
        # Manual income export
        # Loaded only after button click
        # -----------------------------
        manual_income_export_df = cached_income_entries(
            phone=phone,
            start_date=sql_start_date,
            end_date=sql_end_date,
            limit=5000,
        )

        if not manual_income_export_df.empty:
            manual_income_export_df = (
                manual_income_export_df
                .reset_index(drop=True)
                .copy()
            )

            if "id" in manual_income_export_df.columns:
                manual_income_export_df.insert(
                    0,
                    "Income Reference",
                    "INC-" + manual_income_export_df["id"].astype(str)
                )

            manual_income_export_df = manual_income_export_df.rename(
                columns={
                    "income_date": "Date",
                    "customer_name": "Customer",
                    "event_name": "Event / Order",
                    "income_category": "Income Category",
                    "description": "Description",
                    "amount": "Amount",
                    "payment_method": "Payment Method",
                    "currency": "Currency",
                    "source": "Source",
                    "created_at": "Created At",
                }
            )

            manual_income_export_df = manual_income_export_df.drop(
                columns=["id"],
                errors="ignore"
            )

        # -----------------------------
        # Petpooja export
        # -----------------------------
        petpooja_export_df = petpooja_saved_df.copy()

        if not petpooja_export_df.empty:
            petpooja_export_df = petpooja_export_df.drop(
                columns=["date_parsed"],
                errors="ignore"
            )

        # -----------------------------
        # Write Excel workbook
        # -----------------------------
        with pd.ExcelWriter(
            output,
            engine="openpyxl"
        ) as writer:

            if not expense_export_df.empty:
                expense_export_df.to_excel(
                    writer,
                    index=False,
                    sheet_name="Expenses"
                )
            else:
                pd.DataFrame(
                    columns=[
                        "Expense Reference",
                        "Date",
                        "Vendor",
                        "Description",
                        "Category",
                        "Amount",
                    ]
                ).to_excel(
                    writer,
                    index=False,
                    sheet_name="Expenses"
                )

            if not petpooja_export_df.empty:
                petpooja_export_df.to_excel(
                    writer,
                    index=False,
                    sheet_name="Petpooja Sales"
                )
            else:
                pd.DataFrame().to_excel(
                    writer,
                    index=False,
                    sheet_name="Petpooja Sales"
                )

            if not manual_income_export_df.empty:
                manual_income_export_df.to_excel(
                    writer,
                    index=False,
                    sheet_name="Manual Income"
                )
            else:
                pd.DataFrame(
                    columns=[
                        "Income Reference",
                        "Date",
                        "Customer",
                        "Event / Order",
                        "Income Category",
                        "Description",
                        "Amount",
                        "Payment Method",
                        "Currency",
                    ]
                ).to_excel(
                    writer,
                    index=False,
                    sheet_name="Manual Income"
                )

            pd.DataFrame(
                [
                    {
                        "Metric": "Petpooja Income",
                        "Amount": petpooja_filtered_income,
                    },
                    {
                        "Metric": "Manual Income",
                        "Amount": manual_income_total,
                    },
                    {
                        "Metric": "Total Income",
                        "Amount": total_income,
                    },
                    {
                        "Metric": "Total Expense",
                        "Amount": total_expense,
                    },
                    {
                        "Metric": "Net",
                        "Amount": net_amount,
                    },
                ]
            ).to_excel(
                writer,
                index=False,
                sheet_name="Totals"
            )

        output.seek(0)

        st.download_button(
            "⬇️ Download Excel",
            data=output,
            file_name="finwise_financial_report.xlsx",
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            use_container_width=True,
            key="download_finwise_excel"
        )

elif screen == "📁 Folder View":

    st.subheader("📁 File Explorer")

    
    df = cached_load_entries_for_user(phone, limit=1000)

    if not df.empty and "is_deleted" in df.columns:
        deleted_values = (
            df["is_deleted"]
            .fillna("no")
            .astype(str)
            .str.strip()
            .str.lower()
        )

        df = df[
            ~deleted_values.isin(["yes", "true", "1"])
        ].copy()

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

        with st.expander(f"📁 {folder}  •  {folder_count} bills  •  {format_currency(folder_total, phone)}"):

            vendors = sorted(
                folder_df["vendor"].fillna("Unknown Vendor").astype(str).unique()
            )

            for vendor in vendors:

                vendor_df = folder_df[
                    folder_df["vendor"].fillna("Unknown Vendor").astype(str) == vendor
                ].copy()

                vendor_total = vendor_df["total"].sum()
                vendor_count = len(vendor_df)

                with st.expander(f"🏪 {vendor}  •  {vendor_count} bills  •  {format_currency(vendor_total, phone)}"):

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
                                    Date: {bill_date} &nbsp; | &nbsp; Amount: {format_currency(total, phone)}
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

elif screen == "📈 Month-over-Month Analysis":

    st.subheader("📈 Month-over-Month Analysis")

    st.caption(
        "Review every month in a selected year and compare "
        "any two months."
    )

    current_year = date.today().year

    available_years = list(
        range(current_year, current_year - 6, -1)
    )

    selected_year = st.selectbox(
        "Year",
        available_years,
        index=0,
        key="mom_selected_year",
    )

    with st.spinner("Loading..."):
        monthly_df = cached_monthly_analysis(
            phone,
            selected_year
        )

    if monthly_df.empty:
        st.warning(
            "Monthly analysis could not be loaded."
        )
        st.stop()

    numeric_columns = [
        "expense",
        "entries_income",
        "manual_income",
        "petpooja_income",
        "total_income",
        "net",
        "bill_count",
    ]

    for column in numeric_columns:
        if column in monthly_df.columns:
            monthly_df[column] = pd.to_numeric(
                monthly_df[column],
                errors="coerce"
            ).fillna(0)

    monthly_df["month_name"] = monthly_df[
        "month_number"
    ].apply(
        lambda value: calendar.month_abbr[
            int(value)
        ]
    )

    monthly_df["month_full_name"] = monthly_df[
        "month_number"
    ].apply(
        lambda value: calendar.month_name[
            int(value)
        ]
    )

    hide_empty_months = st.checkbox(
        "Hide months without data",
        value=True,
        key="mom_hide_empty_months",
    )

    if hide_empty_months:
        displayed_months_df = monthly_df[
            monthly_df.apply(
                month_has_data,
                axis=1
            )
        ].copy()
    else:
        displayed_months_df = monthly_df.copy()

    st.markdown(
        f"### Monthly Overview — {selected_year}"
    )

    if displayed_months_df.empty:
        st.info(
            f"No financial data was found for "
            f"{selected_year}."
        )

    else:
        monthly_rows = displayed_months_df.to_dict(
            "records"
        )

        for start_index in range(
            0,
            len(monthly_rows),
            4
        ):
            month_columns = st.columns(4)

            current_group = monthly_rows[
                start_index:start_index + 4
            ]

            for column, month_row in zip(
                month_columns,
                current_group
            ):
                month_number = int(
                    month_row["month_number"]
                )

                month_name = calendar.month_name[
                    month_number
                ]

                total_income_value = float(
                    month_row.get(
                        "total_income",
                        0
                    )
                )

                expense_value = float(
                    month_row.get(
                        "expense",
                        0
                    )
                )

                net_value = float(
                    month_row.get(
                        "net",
                        0
                    )
                )

                bill_count = int(
                    month_row.get(
                        "bill_count",
                        0
                    )
                )

                with column:
                    with st.container(border=True):
                        st.markdown(
                            f"#### {month_name} "
                            f"{selected_year}"
                        )

                        st.markdown(
                            f"**Income**  \n"
                            + format_currency(total_income_value, phone)
                        )

                        st.markdown(
                            f"**Expense**  \n"
                            + format_currency(expense_value, phone)
                        )

                        st.markdown(
                            f"**Net**  \n"
                            + format_currency(net_value, phone)
                        )

                        st.caption(
                            f"{bill_count:,} expense "
                            f"bill(s)"
                        )

                        with st.expander(
                            "📁 View expense folders"
                        ):
                            breakdown_df = (
                                cached_month_expense_breakdown(
                                    phone=phone,
                                    year=selected_year,
                                    month=month_number,
                                )
                            )

                            if breakdown_df.empty:
                                st.caption(
                                    "No expense folders "
                                    "for this month."
                                )

                            else:
                                breakdown_df["amount"] = (
                                    pd.to_numeric(
                                        breakdown_df[
                                            "amount"
                                        ],
                                        errors="coerce"
                                    ).fillna(0)
                                )

                                breakdown_df[
                                    "bill_count"
                                ] = pd.to_numeric(
                                    breakdown_df[
                                        "bill_count"
                                    ],
                                    errors="coerce"
                                ).fillna(0).astype(int)

                                for _, category_row in (
                                    breakdown_df.iterrows()
                                ):
                                    st.markdown(
                                        f"📁 **"
                                        f"{category_row['category']}"
                                        f"**  \n"
                                        f"{format_currency(category_row['amount'], phone)}"
                                        f" · "
                                        f"{category_row['bill_count']}"
                                        f" bill(s)"
                                    )

    st.divider()
    st.markdown("### Compare Months")

    months_with_data = monthly_df[
        monthly_df.apply(
            month_has_data,
            axis=1
        )
    ].copy()

    if len(months_with_data) < 2:
        st.info(
            "At least two months with financial data "
            "are required for comparison."
        )

    else:
        month_options = {
            (
                f"{calendar.month_name[int(row['month_number'])]} "
                f"{selected_year}"
            ): int(row["month_number"])
            for _, row in months_with_data.iterrows()
        }

        month_labels = list(month_options.keys())

        default_base_index = max(
            0,
            len(month_labels) - 2
        )

        default_comparison_index = (
            len(month_labels) - 1
        )

        compare_col1, compare_col2, compare_col3 = (
            st.columns([2, 2, 1])
        )

        with compare_col1:
            base_month_label = st.selectbox(
                "Base Month",
                month_labels,
                index=default_base_index,
                key="mom_base_month",
            )

        with compare_col2:
            comparison_month_label = st.selectbox(
                "Compare With",
                month_labels,
                index=default_comparison_index,
                key="mom_comparison_month",
            )

        with compare_col3:
            st.write("")
            st.write("")

            run_comparison = st.button(
                "Run Comparison",
                type="primary",
                use_container_width=True,
                key="mom_run_comparison",
            )

        base_month = month_options[
            base_month_label
        ]

        comparison_month = month_options[
            comparison_month_label
        ]

        if run_comparison:
            st.session_state[
                "mom_active_comparison"
            ] = {
                "year": selected_year,
                "base_month": base_month,
                "comparison_month": comparison_month,
            }

        active_comparison = st.session_state.get(
            "mom_active_comparison"
        )

        if (
            active_comparison
            and active_comparison.get("year")
                == selected_year
        ):
            active_base_month = int(
                active_comparison["base_month"]
            )

            active_comparison_month = int(
                active_comparison[
                    "comparison_month"
                ]
            )

            if (
                active_base_month
                == active_comparison_month
            ):
                st.warning(
                    "Select two different months."
                )

            else:
                base_row = monthly_df[
                    monthly_df["month_number"]
                    == active_base_month
                ].iloc[0]

                comparison_row = monthly_df[
                    monthly_df["month_number"]
                    == active_comparison_month
                ].iloc[0]

                base_month_name = (
                    calendar.month_name[
                        active_base_month
                    ]
                )

                comparison_month_name = (
                    calendar.month_name[
                        active_comparison_month
                    ]
                )

                st.caption(
                    f"Comparing {base_month_name} "
                    f"{selected_year} with "
                    f"{comparison_month_name} "
                    f"{selected_year}."
                )

                income_change = (
                    calculate_percentage_change(
                        base_row["total_income"],
                        comparison_row[
                            "total_income"
                        ],
                    )
                )

                expense_change = (
                    calculate_percentage_change(
                        base_row["expense"],
                        comparison_row["expense"],
                    )
                )

                net_change = (
                    calculate_percentage_change(
                        base_row["net"],
                        comparison_row["net"],
                    )
                )

                bills_change = (
                    calculate_percentage_change(
                        base_row["bill_count"],
                        comparison_row[
                            "bill_count"
                        ],
                    )
                )

                st.markdown(
                    "### Expense Category Comparison"
                )

                st.caption(
                    f"See how spending was distributed across "
                    f"categories in {base_month_name} and "
                    f"{comparison_month_name}."
                )

                category_comparison_df = (
                    cached_month_category_comparison(
                        phone=phone,
                        year=selected_year,
                        base_month=active_base_month,
                        comparison_month=(
                            active_comparison_month
                        ),
                    )
                )

                base_pie_df = prepare_expense_pie_data(
                    category_comparison_df=(
                        category_comparison_df
                    ),
                    amount_column="base_amount",
                )

                comparison_pie_df = prepare_expense_pie_data(
                    category_comparison_df=(
                        category_comparison_df
                    ),
                    amount_column="comparison_amount",
                )

                base_chart_column, comparison_chart_column = (
                    st.columns(2)
                )

                with base_chart_column:
                    with st.container(border=True):
                        base_figure = create_expense_pie_chart(
                            pie_df=base_pie_df,
                            month_name=base_month_name,
                            year=selected_year,
                            phone_number=phone,
                        )

                        st.pyplot(
                            base_figure,
                            width="stretch",
                        )

                        plt.close(base_figure)

                        st.caption(
                            "Total expenses: "
                            + format_currency(float(base_row['expense']), phone)
                        )

                with comparison_chart_column:
                    with st.container(border=True):
                        comparison_figure = (
                            create_expense_pie_chart(
                                pie_df=comparison_pie_df,
                                month_name=(
                                    comparison_month_name
                                ),
                                year=selected_year,
                                phone_number=phone,
                            )
                        )

                        st.pyplot(
                            comparison_figure,
                            width="stretch",
                        )

                        plt.close(comparison_figure)

                        st.caption(
                            "Total expenses: "
                            + format_currency(float(comparison_row['expense']), phone)
                        )
            

                st.markdown(
                    "### AI Detailed Insights"
                )

                insight_col1, insight_col2, \
                    insight_col3 = st.columns(3)

                with insight_col1:
                    with st.container(border=True):
                        st.markdown(
                            "#### Top Expense Changes"
                        )

                        if (
                            category_comparison_df.empty
                        ):
                            st.caption(
                                "No category comparison "
                                "is available."
                            )

                        else:
                            change_rows = []

                            for _, category_row in (
                                category_comparison_df
                                .iterrows()
                            ):
                                base_amount = float(
                                    category_row.get(
                                        "base_amount",
                                        0
                                    )
                                    or 0
                                )

                                compare_amount = float(
                                    category_row.get(
                                        "comparison_amount",
                                        0
                                    )
                                    or 0
                                )

                                percentage = (
                                    calculate_percentage_change(
                                        base_amount,
                                        compare_amount,
                                    )
                                )

                                if (
                                    base_amount > 0
                                    and compare_amount > 0
                                    and percentage
                                        is not None
                                ):
                                    change_rows.append({
                                        "category": (
                                            category_row[
                                                "category"
                                            ]
                                        ),
                                        "base": base_amount,
                                        "comparison": (
                                            compare_amount
                                        ),
                                        "change": (
                                            percentage
                                        ),
                                        "absolute_change": (
                                            abs(percentage)
                                        ),
                                    })

                            change_rows = sorted(
                                change_rows,
                                key=lambda item: (
                                    item[
                                        "absolute_change"
                                    ]
                                ),
                                reverse=True,
                            )[:6]

                            if not change_rows:
                                st.caption(
                                    "No comparable expense "
                                    "categories were found."
                                )

                            for change_row in change_rows:
                                direction_icon = (
                                    "🔺"
                                    if change_row[
                                        "change"
                                    ] > 0
                                    else "🔻"
                                )

                                st.markdown(
                                    f"{direction_icon} **"
                                    f"{change_row['category']}"
                                    f"**  \n"
                                    f"{format_currency(change_row['base'], phone)}"
                                    f" → "
                                    f"{format_currency(change_row['comparison'], phone)}"
                                    f"  \n"
                                    f"{change_row['change']:+.1f}%"
                                )

                with insight_col2:
                    with st.container(border=True):
                        st.markdown(
                            "#### New / Missing Expenses"
                        )

                        if (
                            category_comparison_df.empty
                        ):
                            st.caption(
                                "No missing-expense "
                                "information is available."
                            )

                        else:
                            missing_messages = []

                            for _, category_row in (
                                category_comparison_df
                                .iterrows()
                            ):
                                category_name = str(
                                    category_row.get(
                                        "category",
                                        "Uncategorized"
                                    )
                                )

                                base_amount = float(
                                    category_row.get(
                                        "base_amount",
                                        0
                                    )
                                    or 0
                                )

                                comparison_amount = float(
                                    category_row.get(
                                        "comparison_amount",
                                        0
                                    )
                                    or 0
                                )

                                if (
                                    base_amount > 0
                                    and comparison_amount
                                        == 0
                                ):
                                    missing_messages.append(
                                        (
                                            "⚠️ No "
                                            f"**{category_name}** "
                                            "bill was recorded in "
                                            f"{comparison_month_name}. "
                                            f"{base_month_name}: "
                                            f"{format_currency(base_amount, phone)}."
                                        )
                                    )

                                elif (
                                    base_amount == 0
                                    and comparison_amount > 0
                                ):
                                    missing_messages.append(
                                        (
                                            "➕ **"
                                            f"{category_name}"
                                            "** appeared in "
                                            f"{comparison_month_name}. "
                                            f"Amount: "
                                            f"{format_currency(comparison_amount, phone)}."
                                        )
                                    )

                            if not missing_messages:
                                st.success(
                                    "No new or missing "
                                    "expense categories."
                                )

                            for message in (
                                missing_messages[:8]
                            ):
                                st.markdown(message)

                with insight_col3:
                    with st.container(border=True):
                        st.markdown(
                            "#### Key Observations"
                        )

                        observations = []

                        base_income = float(
                            base_row["total_income"]
                        )

                        comparison_income = float(
                            comparison_row[
                                "total_income"
                            ]
                        )

                        base_expense = float(
                            base_row["expense"]
                        )

                        comparison_expense = float(
                            comparison_row[
                                "expense"
                            ]
                        )

                        base_net = float(
                            base_row["net"]
                        )

                        comparison_net = float(
                            comparison_row["net"]
                        )

                        base_bills = int(
                            base_row["bill_count"]
                        )

                        comparison_bills = int(
                            comparison_row[
                                "bill_count"
                            ]
                        )

                        if (
                            comparison_income
                                > base_income
                            and comparison_expense
                                < base_expense
                        ):
                            observations.append(
                                "✅ Income increased while "
                                "expenses decreased."
                            )

                        if comparison_net > base_net:
                            observations.append(
                                "✅ Net improved from "
                                f"{base_month_name} to "
                                f"{comparison_month_name}."
                            )
                        elif comparison_net < base_net:
                            observations.append(
                                "⚠️ Net declined from "
                                f"{base_month_name} to "
                                f"{comparison_month_name}."
                            )

                        if (
                            comparison_bills < base_bills
                            and comparison_expense
                                > base_expense
                        ):
                            observations.append(
                                "⚠️ Fewer bills were "
                                "recorded, but total expense "
                                "increased. Average bill size "
                                "was higher."
                            )

                        if (
                            comparison_income
                                > base_income
                            and comparison_net
                                <= base_net
                        ):
                            observations.append(
                                "⚠️ Income increased, but "
                                "net did not improve because "
                                "expenses also increased."
                            )

                        if not observations:
                            observations.append(
                                "ℹ️ Performance was broadly "
                                "stable between the selected "
                                "months."
                            )

                        for observation in observations:
                            st.markdown(observation)

                st.caption(
                    "Percentages are calculated from your "
                    "stored financial data. A missing bill "
                    "may mean that the bill was not uploaded."
                )

                st.divider()

                st.markdown(
                    "### 💬 Ask FinWise About Your Data"
                )

                st.caption(
                    "Ask about income, expenses, vendors, "
                    "categories, Petpooja sales, missing "
                    "bills or month-over-month changes."
                )

                comparison_chat_key = (
                    f"finwise_chat_"
                    f"{clean_phone(phone)}_"
                    f"{selected_year}_"
                    f"{active_base_month}_"
                    f"{active_comparison_month}"
                )

                if (
                    comparison_chat_key
                    not in st.session_state
                ):
                    st.session_state[
                        comparison_chat_key
                    ] = []

                chat_messages = (
                    st.session_state[
                        comparison_chat_key
                    ]
                )

                suggestion_col1, suggestion_col2, \
                    suggestion_col3 = st.columns(3)

                suggested_question = None

                with suggestion_col1:
                    if st.button(
                        "Why did expenses change?",
                        key=(
                            "ask_expense_change_"
                            f"{selected_year}_"
                            f"{active_base_month}_"
                            f"{active_comparison_month}"
                        ),
                        width="stretch",
                    ):
                        suggested_question = (
                            "Why did expenses change "
                            f"from {base_month_name} "
                            f"to {comparison_month_name}?"
                        )

                with suggestion_col2:
                    if st.button(
                        "Which bills may be missing?",
                        key=(
                            "ask_missing_bills_"
                            f"{selected_year}_"
                            f"{active_base_month}_"
                            f"{active_comparison_month}"
                        ),
                        width="stretch",
                    ):
                        suggested_question = (
                            "Which expense categories or "
                            "bills may be missing in "
                            f"{comparison_month_name} "
                            f"compared with "
                            f"{base_month_name}?"
                        )

                with suggestion_col3:
                    if st.button(
                        "Did business performance improve?",
                        key=(
                            "ask_performance_"
                            f"{selected_year}_"
                            f"{active_base_month}_"
                            f"{active_comparison_month}"
                        ),
                        width="stretch",
                    ):
                        suggested_question = (
                            "Did business performance "
                            f"improve from "
                            f"{base_month_name} to "
                            f"{comparison_month_name}? "
                            "Explain using income, expenses "
                            "and net."
                        )

                for message in chat_messages:
                    role = message.get(
                        "role",
                        "assistant",
                    )

                    content = message.get(
                        "content",
                        "",
                    )

                    with st.chat_message(role):
                        st.markdown(content)

                typed_question = st.chat_input(
                    (
                        "Ask anything about your "
                        "FinWise financial data..."
                    ),
                    key=(
                        "finwise_comparison_chat_input_"
                        f"{selected_year}_"
                        f"{active_base_month}_"
                        f"{active_comparison_month}"
                    ),
                )

                user_question = (
                    suggested_question
                    or typed_question
                )

                if user_question:
                    chat_messages.append({
                        "role": "user",
                        "content": user_question,
                    })

                    with st.chat_message("user"):
                        st.markdown(user_question)

                    with st.chat_message(
                        "assistant"
                    ):
                        with st.spinner(
                            "FinWise is analysing "
                            "your data..."
                        ):
                            try:
                                financial_context = (
                                    build_finwise_chat_context(
                                        phone=phone,
                                        selected_year=(
                                            selected_year
                                        ),
                                        base_month=(
                                            active_base_month
                                        ),
                                        comparison_month=(
                                            active_comparison_month
                                        ),
                                        monthly_df=monthly_df,
                                        category_comparison_df=(
                                            category_comparison_df
                                        ),
                                    )
                                )

                                answer = (
                                    answer_finwise_data_question(
                                        question=(
                                            user_question
                                        ),
                                        financial_context=(
                                            financial_context
                                        ),
                                        conversation_history=(
                                            chat_messages
                                        ),
                                    )
                                )

                                st.markdown(answer)

                                chat_messages.append({
                                    "role": "assistant",
                                    "content": answer,
                                })

                                st.session_state[
                                    comparison_chat_key
                                ] = chat_messages

                            except Exception as exc:
                                error_message = (
                                    "FinWise could not answer "
                                    "this question. "
                                    f"Error: {exc}"
                                )

                                st.error(error_message)

                if chat_messages:
                    if st.button(
                        "Clear Chat",
                        key=(
                            "clear_finwise_chat_"
                            f"{selected_year}_"
                            f"{active_base_month}_"
                            f"{active_comparison_month}"
                        ),
                    ):
                        st.session_state[
                            comparison_chat_key
                        ] = []

                        st.rerun()

elif screen == "🗑️ Recently Deleted":
    st.subheader("🗑️ Recently Deleted")
    st.caption(
        "Deleted records remain here for 30 days. "
        "You can restore them or permanently delete them."
    )

    # Clean up anything older than 30 days.
    try:
        purged_count = purge_expired_deleted_entries()

        if purged_count > 0:
            st.info(
                f"Permanently removed {purged_count} record(s) "
                f"that were deleted more than 30 days ago."
            )
    except Exception as e:
        st.warning(f"Automatic cleanup could not run: {str(e)}")

    deleted_df = load_recently_deleted_entries(
        owner_phone=phone,
        limit=500
    )

    if deleted_df.empty:
        st.success("Recently Deleted is empty.")

    else:
        deleted_df["total"] = pd.to_numeric(
            deleted_df.get("total", 0),
            errors="coerce"
        ).fillna(0)

        deleted_df["deleted_at"] = pd.to_datetime(
            deleted_df.get("deleted_at"),
            errors="coerce"
        )

        st.metric(
            "Records in Recently Deleted",
            len(deleted_df)
        )

        for _, row in deleted_df.iterrows():
            entry_id = str(row.get("id", ""))
            transaction_type = str(
                row.get("transaction_type", "expense")
            ).title()
            vendor = str(row.get("vendor", ""))
            description = str(row.get("description", ""))
            category = str(row.get("category", ""))
            amount = float(row.get("total", 0) or 0)
            entry_date = str(row.get("date", ""))
            deleted_at = row.get("deleted_at")
            deleted_by = str(row.get("deleted_by", ""))
            delete_source = str(row.get("delete_source", ""))
            days_remaining = int(
                pd.to_numeric(
                    row.get("days_remaining", 0),
                    errors="coerce"
                ) or 0
            )

            reference = f"EXP-{entry_id}"

            with st.expander(
                f"{reference} • {transaction_type} • "
                f"{vendor} • {format_currency(amount, phone)}",
                expanded=False
            ):
                st.write(f"**Reference:** {reference}")
                st.write(f"**Transaction date:** {entry_date}")
                st.write(f"**Type:** {transaction_type}")
                st.write(f"**Vendor / Customer:** {vendor}")
                st.write(f"**Description:** {description}")
                st.write(f"**Category:** {category}")
                st.write(f"**Amount:** {format_currency(amount, phone)}")

                if pd.notna(deleted_at):
                    st.write(
                        "**Deleted on:** "
                        f"{deleted_at.strftime('%d-%b-%Y %I:%M %p')}"
                    )

                if deleted_by:
                    st.write(f"**Deleted by:** {deleted_by}")

                if delete_source:
                    st.write(
                        f"**Delete source:** {delete_source.title()}"
                    )

                st.warning(
                    f"{days_remaining} day(s) remaining before "
                    f"automatic permanent deletion."
                )

                restore_col, permanent_col = st.columns(2)

                with restore_col:
                    if st.button(
                        "♻️ Restore",
                        key=f"restore_deleted_{entry_id}",
                        use_container_width=True
                    ):
                        restored = restore_deleted_entry(
                            entry_id=entry_id,
                            owner_phone=phone,
                        )

                        if restored:
                            st.success(
                                f"{reference} restored successfully."
                            )
                            st.cache_data.clear()
                            st.rerun()
                        else:
                            st.warning(
                                "This record could not be restored."
                            )

                with permanent_col:
                    confirm_key = (
                        f"confirm_permanent_delete_{entry_id}"
                    )

                    confirm_delete = st.checkbox(
                        "Confirm permanent deletion",
                        key=confirm_key,
                    )

                    if st.button(
                        "🗑️ Delete Permanently",
                        key=f"permanent_delete_{entry_id}",
                        use_container_width=True,
                        disabled=not confirm_delete,
                    ):
                        deleted = permanently_delete_entry(
                            entry_id=entry_id,
                            owner_phone=phone,
                        )

                        if deleted:
                            st.success(
                                f"{reference} permanently deleted."
                            )
                            st.cache_data.clear()
                            st.rerun()
                        else:
                            st.warning(
                                "This record could not be deleted."
                            )

elif screen == "⚙️ Settings":
    st.subheader("⚙️ Settings")
    st.caption("Manage your business profile, staff uploaders, and Categories.")

    tab1, tab2, tab3 = st.tabs([
    "🏪 Business Profile",
    "👥 WhatsApp Uploaders",
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


    with tab3:
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

        st.markdown("### 💰 Category Monthly Limits")

        st.caption(
            "Set the maximum monthly spending limit for "
            "each expense category."
        )

        with st.expander(
            "💰 Set Limit for Category",
            expanded=False,
        ):
            budget_categories = (
                get_category_options_for_user(phone)
            )

            selected_budget_category = st.selectbox(
                "Select Category",
                budget_categories,
                key="budget_category_select",
            )

            category_monthly_limit = st.number_input(
                "Monthly Limit",
                min_value=0.0,
                step=500.0,
                format="%.2f",
                key="category_monthly_limit",
            )

            if st.button(
                "💾 Save Category Limit",
                type="primary",
                use_container_width=True,
                key="save_category_limit",
            ):
                if not selected_budget_category:
                    st.error(
                        "Please select a category."
                    )

                elif category_monthly_limit <= 0:
                    st.error(
                        "Please enter a limit greater than 0."
                    )

                else:
                    upsert_category_budget(
                        user_phone=phone,
                        category_name=(
                            selected_budget_category
                        ),
                        monthly_limit=(
                            category_monthly_limit
                        ),
                    )

                    st.success(
                        f"Monthly limit saved for "
                        f"{selected_budget_category}: "
                        + format_currency(category_monthly_limit, phone)
                    )

                    st.cache_data.clear()
                    st.rerun()

        category_budgets_df = (
            load_category_budgets(phone)
        )

        if category_budgets_df.empty:
            st.info(
                "No category limits have been set yet."
            )

        else:
            st.markdown(
                "#### Current Category Limits"
            )

            for _, budget_row in (
                category_budgets_df.iterrows()
            ):
                budget_id = int(
                    budget_row.get("id", 0)
                )

                budget_category = str(
                    budget_row.get(
                        "category",
                        ""
                    )
                )

                budget_limit = float(
                    budget_row.get(
                        "monthly_budget",
                        0,
                    )
                    or 0
                )

                limit_col1, limit_col2, limit_col3 = (
                    st.columns([4, 3, 1])
                )

                with limit_col1:
                    st.write(
                        f"**{budget_category}**"
                    )

                with limit_col2:
                    st.write(
                        f"{format_currency(budget_limit, phone)} / month"
                    )

                with limit_col3:
                    if st.button(
                        "Delete",
                        key=(
                            f"delete_category_budget_"
                            f"{budget_id}"
                        ),
                    ):
                        deleted = (
                            delete_category_budget(
                                budget_id=budget_id,
                                user_phone=phone,
                            )
                        )

                        if deleted:
                            st.success(
                                "Category limit deleted."
                            )

                            st.cache_data.clear()
                            st.rerun()

                        else:
                            st.warning(
                                "Category limit could "
                                "not be deleted."
                            )

                st.divider()

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

elif screen == "💡 Help Shape FinWise":
    st.subheader("💡 Help Shape FinWise")

    st.caption(
        "FinWise is currently in beta. Tell us what would make "
        "it more useful for your business."
    )

    with st.form("finwise_feedback_form", clear_on_submit=True):
        feedback_rating = st.slider(
            "How would you rate your FinWise experience?",
            min_value=1,
            max_value=5,
            value=5,
        )

        feedback_feature = st.text_area(
            "What feature would you love to see next?",
            placeholder=(
                "Example: vendor price comparison, automatic daily "
                "briefing, bank statement upload..."
            ),
        )

        feedback_improvement = st.text_area(
            "What should we improve?",
            placeholder=(
                "Tell us about anything confusing, slow or difficult "
                "to use."
            ),
        )

        feedback_contact_permission = st.checkbox(
            "You may contact me on my registered WhatsApp number "
            "about this feedback.",
            value=False,
        )

        feedback_submitted = st.form_submit_button(
            "Submit Feedback",
            type="primary",
            use_container_width=True,
        )

    if feedback_submitted:
        if (
            not feedback_feature.strip()
            and not feedback_improvement.strip()
        ):
            st.warning(
                "Please share a feature request or an improvement."
            )
        else:
            try:
                with st.spinner("Submitting feedback..."):
                    save_finwise_feedback(
                        user_phone=phone,
                        rating=feedback_rating,
                        feature_request=feedback_feature,
                        improvement=feedback_improvement,
                        contact_permission=(
                            feedback_contact_permission
                        ),
                    )

                st.success(
                    "Thank you! Your feedback will help shape FinWise."
                )
            except Exception as exc:
                st.error(
                    "Feedback could not be submitted right now. "
                    f"Error: {exc}"
                )


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
