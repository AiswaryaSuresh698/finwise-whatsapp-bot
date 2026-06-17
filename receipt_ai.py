import base64
import json
from io import BytesIO
from PIL import Image
from openai import OpenAI
import time


CATEGORY_OPTIONS = [
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


def get_client(api_key: str):
    return OpenAI(api_key=api_key)


def image_to_base64(image: Image.Image) -> str:

    image = image.convert("RGB")

    # Resize large phone images
    image.thumbnail((1200, 1200))

    buffer = BytesIO()

    # JPEG is much smaller than PNG
    image.save(
        buffer,
        format="JPEG",
        quality=75,
        optimize=True
    )

    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def extract_bill_details(client, image: Image.Image, model: str = "gpt-4.1-mini") -> dict:
    image_b64 = image_to_base64(image)

    image_size_mb = len(base64.b64decode(image_b64)) / (1024 * 1024)
    print(f"IMAGE SIZE SENT TO OPENAI: {image_size_mb:.2f} MB")

    prompt = """
Analyze this bill, receipt, invoice, or payment document.

Extract details for a simple bookkeeping table.

Also predict which folder the original bill image should be saved into.

Folder rules:
- Grocery: Devapaul Supermarket, Cheran Vegetables & Fruits
- Gas: fuel, gas station, Petro Canada, Shell, Esso
- Internet: Bell, Rogers, Telus, Fido, Videotron, mobile, internet
- Utilities: Hydro, electricity, water, utility bills
- Meals: restaurant, cafe, food, delivery
- Rent: rent, lease
- Software: SaaS, OpenAI, Adobe, Microsoft, Canva, GitHub, Notion
- Office Supplies: stationery, office items, printer, paper
- Vehicle: parking, repair, maintenance, car wash
- Professional Fees: accountant, legal, consulting
- Insurance: insurance bills
- Travel: hotel, flight, train, taxi
- Income: invoice issued to customer/client
- Uncategorized: unclear or personal-looking items

Rules:
- If unsure, set folder = Uncategorized.
- Do not force uncertain retail/beauty/clothing receipts into Meals.
- Winners, Marshalls, HomeSense, beauty salons, cosmetics = Uncategorized unless clearly business-related.
- Return only valid JSON.
"""

    schema = {
        "name": "bill_extraction",
        "schema": {
            "type": "object",
            "properties": {
                "transaction_type": {"type": "string", "enum": ["expense", "income"]},
                "date": {"type": "string"},
                "vendor": {"type": "string"},
                "description": {"type": "string"},
                "category": {"type": "string", "enum": CATEGORY_OPTIONS},
                "folder": {"type": "string", "enum": CATEGORY_OPTIONS},
                "subtotal": {"type": "number"},
                "tax": {"type": "number"},
                "total": {"type": "number"},
                "currency": {"type": "string"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "reason": {"type": "string"},
            },
            "required": [
                "transaction_type",
                "date",
                "vendor",
                "description",
                "category",
                "folder",
                "subtotal",
                "tax",
                "total",
                "currency",
                "confidence",
                "reason",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    }

    start = time.time()

    response = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_schema", "json_schema": schema},
        messages=[
            {
                "role": "system",
                "content": "You are a precise bill and receipt extraction engine.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_b64}",
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
    )

    print(f"OPENAI EXTRACTION: {time.time() - start:.2f}s")

    data = json.loads(response.choices[0].message.content)

    return {
        "transaction_type": data.get("transaction_type", "expense"),
        "date": data.get("date", ""),
        "vendor": data.get("vendor", ""),
        "description": data.get("description", ""),
        "category": data.get("category", "Uncategorized"),
        "folder": data.get("folder", "Uncategorized"),
        "subtotal": float(data.get("subtotal", 0) or 0),
        "tax": float(data.get("tax", 0) or 0),
        "total": float(data.get("total", 0) or 0),
        "currency": data.get("currency", "CAD"),
        "confidence": data.get("confidence", "medium"),
        "reason": data.get("reason", ""),
    }