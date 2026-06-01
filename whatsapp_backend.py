import os
import requests
from io import BytesIO
import json

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
from PIL import Image
from storage_utils import load_entries, save_entries, update_vendor_memory
import pandas as pd
import hashlib
from storage_utils import load_entries

from receipt_ai import get_client, extract_bill_details
from storage_utils import (
    append_entry,
    save_image_to_folder,
    ensure_storage,
    apply_vendor_memory,
)


load_dotenv()
ensure_storage()

app = Flask(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = get_client(OPENAI_API_KEY)

PENDING_CATEGORY_FILE = "data/pending_category.json"


def load_pending_category():
    if not os.path.exists(PENDING_CATEGORY_FILE):
        return {}

    with open(PENDING_CATEGORY_FILE, "r") as f:
        return json.load(f)


def save_pending_category(data):
    with open(PENDING_CATEGORY_FILE, "w") as f:
        json.dump(data, f, indent=2)


def clear_pending_category(phone):
    data = load_pending_category()
    phone = str(phone)

    if phone in data:
        del data[phone]

    save_pending_category(data)


@app.route("/", methods=["GET"])
def home():
    return "FinWise WhatsApp bot is running."

def make_expense_duplicate_key(from_number, extracted):
    vendor = str(extracted.get("vendor", "")).strip().lower()
    date = str(extracted.get("date", "")).strip()
    total = str(round(float(extracted.get("total", 0) or 0), 2))
    description = str(extracted.get("description", "")).strip().lower()

    raw_key = f"{from_number}|{date}|{vendor}|{total}|{description}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def is_duplicate_expense(duplicate_key):
    entries_df = load_entries()

    if entries_df.empty:
        return False

    if "duplicate_key" not in entries_df.columns:
        return False

    return duplicate_key in entries_df["duplicate_key"].astype(str).values

@app.route("/whatsapp", methods=["POST"])
def whatsapp():

    print("\n========== NEW WHATSAPP REQUEST ==========")

    from_number = request.form.get("From")
    body = request.form.get("Body")
    media_count = request.form.get("NumMedia")
    print("Reached media processing section")

    print("From:", from_number)
    print("Body:", body)
    print("Media count:", media_count)

    response = MessagingResponse()

    from_number = request.form.get("From", "").replace("whatsapp:", "")
    body = request.form.get("Body", "").strip()
    incoming_msg = body
    num_media = media_count

    allowed_categories = [
        "Grocery", "Gas", "Internet", "Utilities", "Meals", "Rent",
        "Software", "Office Supplies", "Vehicle", "Professional Fees",
        "Insurance", "Travel", "Income", "Uncategorized"
    ]

    # TEXT MESSAGE: category reply or normal hello
    if num_media == 0:
        pending = load_pending_category()
        phone_key = str(from_number)

        if phone_key in pending:
            category = incoming_msg.title()

            if not category:
                response.message(
                    "Please reply with a valid category.\n"
                    "Example: Grocery, Gas, Meals, Utilities."
                )
                return str(response)

            if category not in allowed_categories:
                response.message(
                    f"'{category}' is not a valid category.\n\n"
                    "Please reply with one of these:\n"
                    "Grocery, Gas, Internet, Utilities, Meals, Rent, Software, "
                    "Office Supplies, Vehicle, Professional Fees, Insurance, Travel."
                )
                return str(response)

            pending_entry = pending[phone_key]
            pending_entry["category"] = category
            pending_entry["folder"] = category

            append_entry(pending_entry)

            update_vendor_memory(
                user_phone=from_number,
                vendor=pending_entry.get("vendor", ""),
                category=category,
                folder=category,
            )

            clear_pending_category(phone_key)

            response.message(
                f"Saved bill ✅\n"
                f"Vendor: {pending_entry.get('vendor', '')}\n"
                f"Total: ₹{pending_entry.get('total', '')}\n"
                f"Category saved as: {category}"
            )
            return str(response)

        response.message("Send a bill/receipt image here.")
        return str(response)

    # IMAGE MESSAGE
    media_url = request.form.get("MediaUrl0")
    media_type = request.form.get("MediaContentType0", "")

    print("Media URL:", media_url)
    print("Media Type:", media_type)

    if not media_url:
        response.message("Please upload a bill image.")
        return str(response)

    try:
        media_response = requests.get(
            media_url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=30,
        )
        media_response.raise_for_status()

        image_bytes = media_response.content
        image = Image.open(BytesIO(image_bytes)).convert("RGB")

        extracted = extract_bill_details(client, image)

        if extracted.get("image_quality") in ["blurry", "unreadable"]:
            response.message(
                "This image is not clear enough to read. Please upload a clearer bill photo with good lighting."
            )
            return str(response)

        duplicate_key = make_expense_duplicate_key(from_number, extracted)

        if is_duplicate_expense(duplicate_key):
            response.message("This bill was already uploaded earlier.")
            return str(response)

        vendor = extracted.get("vendor", "")
        category = extracted.get("category", "Uncategorized")
        folder = extracted.get("folder", "Uncategorized")

        memory_category, memory_folder = apply_vendor_memory(from_number, vendor)
        has_vendor_memory = bool(memory_category or memory_folder)

        if memory_category:
            category = memory_category
        if memory_folder:
            folder = memory_folder

        ext = "png" if "png" in media_type else "jpg"

        image_path = save_image_to_folder(
            image_bytes=image_bytes,
            folder=folder,
            vendor=vendor or "unknown",
            ext=ext,
            bill_date=extracted.get("date", "")
        )

        entry = {
            "date": extracted.get("date", ""),
            "transaction_type": extracted.get("transaction_type", "expense"),
            "vendor": vendor,
            "user_phone": from_number,
            "description": extracted.get("description", ""),
            "category": category,
            "folder": folder,
            "subtotal": extracted.get("subtotal", 0),
            "tax": extracted.get("tax", 0),
            "total": extracted.get("total", 0),
            "currency": extracted.get("currency", "CAD"),
            "confidence": extracted.get("confidence", "medium"),
            "reason": extracted.get("reason", ""),
            "image_path": image_path,
            "duplicate_key": duplicate_key,
        }

        if not has_vendor_memory:
            pending = load_pending_category()
            pending[str(from_number)] = entry
            save_pending_category(pending)

            response.message(
                f"I found a new vendor and need category confirmation.\n\n"
                f"Vendor: {entry['vendor']}\n"
                f"Total: ₹{entry['total']}\n\n"
                f"Which category should I save this under?\n"
                f"Example: Grocery, Gas, Meals, Utilities."
            )
            return str(response)

        append_entry(entry)

        response.message(
            f"Saved bill ✅\n"
            f"Vendor: {entry['vendor']}\n"
            f"Total: ₹{entry['total']}\n"
            f"Category: {entry['category']}"
        )

    except Exception as e:
        response.message(f"Could not process the bill. Error: {str(e)}")

    return str(response)


if __name__ == "__main__":
    app.run(port=5000, debug=True)