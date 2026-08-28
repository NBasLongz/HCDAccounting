"""Parse Mira GoodFood receipt PDFs into structured receipt rows."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import pdfplumber


def extract_lines(pdf_path: str) -> list[str]:
    lines: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for line in (page.extract_text() or "").splitlines():
                if line := line.strip():
                    lines.append(line)
    return lines


RECEIPT_ID_RE = re.compile(r"^[A-Za-zÀ-Ỹà-ỹ]{1,6}-\d{1,6}$")
ITEM_RE = re.compile(r"^(\d+)x\s+(.+?)\s+(-?[\d][\d.,]*)\s*₫?$")
QTY_ONLY_RE = re.compile(r"^(\d+)x$")
TRAILING_AMOUNT_RE = re.compile(r"^(.+?)\s+(-?[\d][\d.,]*)\s*₫?$")
PROMO_CODE_RE = re.compile(r"^[A-Z0-9]{6,20}$")
ITEM_COUNT_RE = re.compile(r"^\d+\s*món$")
DISCOUNT_HINTS = ("sale", "giảm giá", "ưu đãi", "khuyến mãi", "🌸", "🔥", "🍀", "🍄")
SKIP_PREFIXES = ("Làm xong đơn trước", "Đơn của", "Tổng tạm tính", "Bao gồm thuế", "Tổng cộng")


@dataclass
class Row:
    type: str
    label: str
    amount: Optional[float] = None


@dataclass
class Receipt:
    receipt_id: str
    display_name: str
    rows: list[Row] = field(default_factory=list)
    pdf_total: Optional[float] = None
    source_file_index: int = 0


def split_receipts(lines: list[str]) -> list[list[str]]:
    starts = [i for i in range(len(lines) - 1) if RECEIPT_ID_RE.match(lines[i]) and lines[i + 1].startswith("Làm xong đơn trước")]
    if not starts:
        return [lines] if lines else []
    starts.append(len(lines))
    return [lines[starts[i]:starts[i + 1]] for i in range(len(starts) - 1)]


def parse_vn_number(raw: str) -> float:
    value = raw.replace("₫", "").strip()
    negative = value.startswith("-")
    if negative:
        value = value[1:]
    if re.search(r",\d{1,2}$", value):
        value = value.replace(".", "").replace(",", ".")
    else:
        value = value.replace(".", "").replace(",", "")
    return -float(value) if negative else float(value)


def parse_receipt(lines: list[str]) -> Receipt:
    if not lines:
        raise ValueError("PDF không có nội dung văn bản")
    receipt_id = lines[0]
    receipt = Receipt(receipt_id=receipt_id, display_name=f"Receipt-{receipt_id}.pdf")
    current_qty: str = "1x"
    in_discount_section = False

    raw_items_with_qty: list[tuple[str, Row]] = []
    discount_rows: list[Row] = []

    for line in lines[1:]:
        if line.startswith("Mira GoodFood"):
            break
        if line == receipt_id or ITEM_COUNT_RE.match(line):
            continue
        if line == "Giảm giá":
            in_discount_section = True
            continue
        if any(line.startswith(prefix) for prefix in SKIP_PREFIXES):
            if line.startswith("Tổng cộng") and (match := TRAILING_AMOUNT_RE.match(line)):
                receipt.pdf_total = parse_vn_number(match.group(2))
            continue
        if match := ITEM_RE.match(line):
            qty_num, name, amount = match.groups()
            current_qty = f"{qty_num}x"
            raw_items_with_qty.append((current_qty, Row("item", name.strip(), parse_vn_number(amount))))
            in_discount_section = False
            continue
        if match := QTY_ONLY_RE.match(line):
            current_qty = f"{match.group(1)}x"
            continue
        if any(hint in line.lower() or hint in line for hint in DISCOUNT_HINTS):
            if match := TRAILING_AMOUNT_RE.match(line):
                discount_rows.append(Row("discount", match.group(1).strip(), parse_vn_number(match.group(2))))
            continue
        if match := TRAILING_AMOUNT_RE.match(line):
            label, raw_amount = match.groups()
            amount = parse_vn_number(raw_amount)
            if in_discount_section and PROMO_CODE_RE.match(label.strip()) and amount > 0:
                amount = -amount
            if amount < 0 or in_discount_section:
                discount_rows.append(Row("discount", label.strip(), amount))
            else:
                raw_items_with_qty.append((current_qty, Row("item", label.strip(), amount)))
            continue
        # Multiline text continuation
        if raw_items_with_qty and not discount_rows:
            last_qty, last_item = raw_items_with_qty[-1]
            last_item.label = f"{last_item.label} {line}".strip()
        elif discount_rows:
            discount_rows[-1].label = f"{discount_rows[-1].label} {line}".strip()

    # Group items by quantity (1x, 2x, 3x...)
    qty_groups: dict[str, list[Row]] = {}
    for q, item in raw_items_with_qty:
        if q not in qty_groups:
            qty_groups[q] = []
        qty_groups[q].append(item)

    def qty_sort_key(q: str) -> int:
        m = re.search(r"\d+", q)
        return int(m.group(0)) if m else 0

    sorted_qtys = sorted(qty_groups.keys(), key=qty_sort_key, reverse=True)

    organized_rows: list[Row] = []
    for q in sorted_qtys:
        organized_rows.append(Row(type="qty", label=q))
        for item in qty_groups[q]:
            organized_rows.append(item)

    # Group discounts by label/program
    discount_map: dict[str, float] = {}
    for d in discount_rows:
        amt = d.amount or 0.0
        if amt > 0:
            amt = -amt
        lbl = d.label.strip()
        discount_map[lbl] = discount_map.get(lbl, 0.0) + amt

    consolidated_discounts = [
        Row(type="discount", label=lbl, amount=round(amt, 3))
        for lbl, amt in discount_map.items()
    ]

    receipt.rows = organized_rows + consolidated_discounts
    return receipt


def parse_pdf(pdf_path: str) -> list[Receipt]:
    return [parse_receipt(part) for part in split_receipts(extract_lines(pdf_path))]
