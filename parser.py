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
QTY_ITEM_START_RE = re.compile(r"^(\d+)x\s+(.+)$")
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

    last_target: Optional[tuple[str, Row]] = None
    pending_item_name: Optional[tuple[str, str]] = None

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

        # 1. Full item on a single line: 1x Name 100.000₫
        if match := ITEM_RE.match(line):
            qty_num, name, amount = match.groups()
            current_qty = f"{qty_num}x"
            item_row = Row("item", name.strip(), parse_vn_number(amount))
            raw_items_with_qty.append((current_qty, item_row))
            last_target = ("item", item_row)
            pending_item_name = None
            in_discount_section = False
            continue

        # 2. Quantity header only: 1x
        if match := QTY_ONLY_RE.match(line):
            current_qty = f"{match.group(1)}x"
            pending_item_name = None
            continue

        # 3. Item starts on line with quantity but amount is wrapped to next line: 1x Name starts here...
        if match := QTY_ITEM_START_RE.match(line):
            qty_num, partial_name = match.groups()
            current_qty = f"{qty_num}x"
            pending_item_name = (current_qty, partial_name.strip())
            continue

        # 4. Discount by keyword/emoji hint: 🌸Flash sale 50% -9.920₫
        if any(hint in line.lower() or hint in line for hint in DISCOUNT_HINTS):
            if match := TRAILING_AMOUNT_RE.match(line):
                disc_row = Row("discount", match.group(1).strip(), parse_vn_number(match.group(2)))
                discount_rows.append(disc_row)
                last_target = ("discount", disc_row)
                pending_item_name = None
            continue

        # 5. Line with trailing amount: Name/Code amount
        if match := TRAILING_AMOUNT_RE.match(line):
            label, raw_amount = match.groups()
            amount = parse_vn_number(raw_amount)
            if in_discount_section and PROMO_CODE_RE.match(label.strip()) and amount > 0:
                amount = -amount

            if amount < 0 or in_discount_section:
                disc_row = Row("discount", label.strip(), amount)
                discount_rows.append(disc_row)
                last_target = ("discount", disc_row)
            else:
                if pending_item_name:
                    p_qty, p_name = pending_item_name
                    full_name = f"{p_name} {label.strip()}".strip()
                    item_row = Row("item", full_name, amount)
                    raw_items_with_qty.append((p_qty, item_row))
                else:
                    item_row = Row("item", label.strip(), amount)
                    raw_items_with_qty.append((current_qty, item_row))
                last_target = ("item", item_row)
            pending_item_name = None
            continue

        # 6. Text-only wrapped continuation line (no price, no qty prefix)
        if pending_item_name:
            p_qty, p_name = pending_item_name
            pending_item_name = (p_qty, f"{p_name} {line.strip()}".strip())
        elif last_target:
            target_type, target_row = last_target
            target_row.label = f"{target_row.label} {line.strip()}".strip()

    # Group items by quantity dynamically (descending order: 3x, 2x, 1x...)
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
