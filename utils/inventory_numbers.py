"""Automatic identifiers used by the inventory module.

The inventory screens still accept a manually supplied code/number for legacy
documents or an external reference.  When the field is left empty, these
helpers generate a stable identifier from the database row id after the row
has been flushed.
"""

from datetime import datetime


MASTER_CODE_PREFIXES = {
    "warehouse": "WH",
    "room": "RM",
}

VOUCHER_PREFIXES = {
    "issue": "ISS",
    "issue_transfer": "TRF",
    "employee": "EMP",
    "inbound": "IN",
    "scrap": "SCR",
    "return": "RET",
    "stocktake": "STK",
    "custody": "CUS",
}


def auto_inventory_code(kind: str, row_id: int) -> str:
    """Return a human-readable code for a warehouse or room."""

    prefix = MASTER_CODE_PREFIXES.get((kind or "").strip().lower(), "INV")
    return f"{prefix}-{int(row_id):04d}"


def auto_inventory_voucher_no(kind: str, voucher_date: str | None, row_id: int) -> str:
    """Return a year-aware document number for an inventory voucher."""

    prefix = VOUCHER_PREFIXES.get((kind or "").strip().lower(), "INV")
    year = str(voucher_date or "")[:4]
    if not year.isdigit() or len(year) != 4:
        year = str(datetime.utcnow().year)
    return f"{prefix}-{year}-{int(row_id):06d}"
