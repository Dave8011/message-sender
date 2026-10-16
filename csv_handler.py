"""
WhatsApp Bulk Sender — CSV Contact Importer
Parses CSV files and normalizes phone numbers for WhatsApp sending.
Supports Shopify exports, Google Contacts, and generic CSV formats.
"""

from __future__ import annotations

import csv
import io
import logging

try:
    import phonenumbers
except ImportError:
    phonenumbers = None

logger = logging.getLogger(__name__)

# Maps common CSV column names to our internal fields
COLUMN_MAP = {
    # Phone
    "phone": "phone",
    "telephone": "phone",
    "tel": "phone",
    "mobile": "phone",
    "numero": "phone",
    "numéro": "phone",
    "numbers": "phone",
    "number": "phone",
    "phone number": "phone",
    "phone_number": "phone",
    "phonenumber": "phone",
    "whatsapp": "phone",
    "contact": "phone",
    "recipient": "phone",
    "destinataire": "phone",
    "profile link": "phone",
    "profile_link": "phone",
    "billing phone": "phone",
    "shipping phone": "phone",
    "address_phone": "phone",
    # First name
    "first_name": "first_name",
    "first name": "first_name",
    "firstname": "first_name",
    "prenom": "first_name",
    "prénom": "first_name",
    "billing first name": "first_name",
    # Last name
    "last_name": "last_name",
    "last name": "last_name",
    "lastname": "last_name",
    "nom": "last_name",
    "billing last name": "last_name",
    # Full name
    "name": "name",
    "full_name": "name",
    "fullname": "name",
    # Email
    "email": "email",
    "e-mail": "email",
    "mail": "email",
    "billing email": "email",
}


def normalize_phone(raw_phone: str, default_region: str = "FR") -> str | None:
    """
    Normalize a phone number to E.164 format (+33612345678).
    Returns None if the number is invalid.
    """
    if not raw_phone or not raw_phone.strip():
        return None

    raw = raw_phone.strip()

    if phonenumbers:
        try:
            parsed = phonenumbers.parse(raw, default_region)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except Exception:
            pass

    # Fallback: if it looks like a number with enough digits
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) >= 8:
        return f"+{digits}"

    return None



def parse_csv(file_content: bytes | str) -> tuple[list[dict], list[str]]:
    """
    Parse a CSV file and extract contacts.
    Uses standard library csv to avoid heavy pandas bundle on serverless platforms.

    Args:
        file_content: Raw CSV content (bytes or string)

    Returns:
        (valid_contacts, errors) where valid_contacts is a list of
        {"first_name": ..., "last_name": ..., "phone": ..., "email": ...}
    """
    # Handle encoding
    if isinstance(file_content, bytes):
        for encoding in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try:
                text = file_content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            return [], ["Impossible de decoder le fichier CSV."]
    else:
        text = file_content

    # shortcut: standard library csv parser avoids 80MB pandas+numpy bundle on serverless Vercel; upgrade path: pandas if complex dataframe transforms needed
    try:
        sample = text[:4096]
        delimiter = ","
        try:
            dialect = csv.Sniffer().sniff(sample)
            if dialect.delimiter in (",", ";", "\t", "|"):
                delimiter = dialect.delimiter
        except Exception:
            if ";" in sample and sample.count(";") > sample.count(","):
                delimiter = ";"
            elif "\t" in sample and sample.count("\t") > sample.count(","):
                delimiter = "\t"

        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        raw_rows = [row for row in reader if any(field.strip() for field in row)]
    except Exception as e:
        return [], [f"Erreur de parsing CSV: {e}"]

    if not raw_rows:
        return [], ["Le fichier CSV est vide."]

    headers = [h.strip() for h in raw_rows[0]]
    data_rows = raw_rows[1:]

    # Map columns
    col_map = {}
    for idx, col in enumerate(headers):
        normalized = col.strip().lower()
        if normalized in COLUMN_MAP:
            internal = COLUMN_MAP[normalized]
            if internal not in col_map:
                col_map[internal] = idx

    # Fallback: if single column or header not recognized, treat column 0 as phone
    if "phone" not in col_map:
        if len(headers) == 1:
            col_map["phone"] = 0
        else:
            return [], [f"Aucune colonne 'phone' trouvee. Colonnes disponibles: {', '.join(headers)}"]

    contacts = []
    errors = []
    seen_phones = set()

    for idx, row in enumerate(data_rows):
        line_num = idx + 2  # +2 for header + 0-index

        phone_idx = col_map["phone"]
        raw_phone = str(row[phone_idx]).strip() if phone_idx < len(row) else ""
        phone = normalize_phone(raw_phone)

        if not phone:
            errors.append(f"Ligne {line_num}: numero invalide '{raw_phone}'")
            continue

        if phone in seen_phones:
            continue
        seen_phones.add(phone)

        # Extract name fields
        first_name = ""
        last_name = ""

        if "first_name" in col_map and col_map["first_name"] < len(row):
            first_name = str(row[col_map["first_name"]]).strip()
        if "last_name" in col_map and col_map["last_name"] < len(row):
            last_name = str(row[col_map["last_name"]]).strip()

        # If we have a single "name" column, split it
        if not first_name and "name" in col_map and col_map["name"] < len(row):
            full_name = str(row[col_map["name"]]).strip()
            parts = full_name.split(None, 1)
            first_name = parts[0] if parts else ""
            last_name = parts[1] if len(parts) > 1 else ""

        email = ""
        if "email" in col_map and col_map["email"] < len(row):
            email = str(row[col_map["email"]]).strip()

        contacts.append({
            "first_name": first_name or "Contact",
            "last_name": last_name,
            "phone": phone,
            "email": email,
        })

    return contacts, errors

