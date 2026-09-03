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


RECEIPT_ID_RE = re.compile(r"^[A-Za-zÀ-Ỹà-ỹ]{1,6}-\d{1,6}[A-Za-z]?$")
ITEM_RE = re.compile(r"^(\d+)x\s+(.+?)\s+(-?[\d][\d.,]*)\s*₫?$")
QTY_ITEM_START_RE = re.compile(r"^(\d+)x\s+(.+)$")
QTY_ONLY_RE = re.compile(r"^(\d+)x$")
TRAILING_AMOUNT_RE = re.compile(r"^(.+?)\s+(-?[\d][\d.,]*)\s*₫?$")
AMOUNT_ONLY_RE = re.compile(r"^(-?[\d][\d.,]*)\s*₫?$")
PROMO_CODE_RE = re.compile(r"^[A-Z0-9]{6,25}$")
ITEM_COUNT_RE = re.compile(r"^\d+\s*món$")
DISCOUNT_HINTS = ("sale", "giảm giá", "ưu đãi", "khuyến mãi", "tặng ngay", "tặng kèm", "trái cây giảm sốc", "đồng tài trợ", "🌸", "🔥", "🍀", "🍄")
CONDITION_KEYWORDS = ("tối thiểu", "đơn từ", "áp dụng cho", "khi đặt đơn", "khi mua")
CANCEL_HINTS = ("đã xoá", "đã xóa", "đã huỷ", "đã hủy", "✗")


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
    starts = [
        i for i in range(len(lines) - 1)
        if RECEIPT_ID_RE.match(lines[i]) and (
            lines[i + 1].startswith("Làm xong đơn trước")
            or lines[i + 1].startswith("Đơn của")
            or lines[i + 1].startswith("Đã hủy")
            or lines[i + 1].startswith("Đã huỷ")
            or lines[i + 1].startswith("*****")
        )
    ]
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


def parse_receipt(lines: list[str]) -> Optional[Receipt]:
    if not lines:
        return None

    # Exclude cancelled receipt completely if header indicates cancellation (Đã hủy / Đã huỷ)
    for line in lines[:15]:
        l_lower = line.strip().lower()
        if (
            "đã hủy" in l_lower
            or "đã huỷ" in l_lower
            or "đơn bị hủy" in l_lower
            or "đơn bị huỷ" in l_lower
            or "đã bị hủy" in l_lower
            or "đã bị huỷ" in l_lower
            or "cancelled" in l_lower
            or "canceled" in l_lower
        ):
            return None

    receipt_id = lines[0]
    receipt = Receipt(receipt_id=receipt_id, display_name=f"Receipt-{receipt_id}.pdf")
    current_qty: str = "1x"

    raw_items_with_qty: list[tuple[str, Row]] = []
    discount_rows: list[Row] = []

    last_target: Optional[tuple[str, Row]] = None
    pending_item_name: Optional[tuple[str, str]] = None
    pending_discount_text: list[str] = []
    in_summary_discount_section = False

    # Extract pdf_total from the full lines of the receipt
    for line in lines:
        if line.startswith("Tổng cộng") and (match := TRAILING_AMOUNT_RE.match(line)):
            receipt.pdf_total = parse_vn_number(match.group(2))

    for line in lines[1:]:
        if line.startswith("Tổng cộng") or line.startswith("Mira GoodFood"):
            break
        if line == receipt_id or ITEM_COUNT_RE.match(line) or line.startswith("Làm xong đơn trước") or line.startswith("Đơn của") or line == "Đã hủy":
            continue
        if line.startswith("Tổng tạm tính") or line.startswith("Bao gồm thuế"):
            continue
        if line == "Giảm giá":
            in_summary_discount_section = True
            continue

        # In summary discount section (between Giảm giá and Tổng cộng):
        if in_summary_discount_section:
            if match := TRAILING_AMOUNT_RE.match(line):
                code_or_lbl, raw_amt = match.groups()
                clean_code = code_or_lbl.strip()
                # Check if this is an order voucher code (e.g. SPLHIG299A5PLZU 30.000₫)
                if PROMO_CODE_RE.match(clean_code):
                    amt = parse_vn_number(raw_amt)
                    if amt > 0:
                        amt = -amt
                    discount_rows.append(Row("discount", clean_code, amt))
            continue

        # Check if this is a cancelled item line
        is_cancelled = any(h in line.lower() for h in CANCEL_HINTS)
        if is_cancelled:
            pending_item_name = None
            continue

        # 1. Full item on a single line: 1x Name 100.000₫
        if match := ITEM_RE.match(line):
            qty_num, name, amount_str = match.groups()
            current_qty = f"{qty_num}x"
            # Cancelled item (0x) should not add price to total
            if qty_num == "0":
                pending_item_name = None
                continue
            item_row = Row("item", name.strip(), parse_vn_number(amount_str))
            raw_items_with_qty.append((current_qty, item_row))
            last_target = ("item", item_row)
            pending_item_name = None
            pending_discount_text = []
            continue

        # 2. Quantity header only: 1x or 0x
        if match := QTY_ONLY_RE.match(line):
            qty_num = match.group(1)
            current_qty = f"{qty_num}x"
            pending_item_name = None
            pending_discount_text = []
            continue

        # 3. Item starts on line with quantity: 1x Name starts here...
        if match := QTY_ITEM_START_RE.match(line):
            qty_num, partial_name = match.groups()
            current_qty = f"{qty_num}x"
            if qty_num == "0":
                pending_item_name = None
                continue
            pending_item_name = (current_qty, partial_name.strip())
            pending_discount_text = []
            continue

        # 4. Amount ONLY line (e.g. "-199.000" or "100.000")
        if match := AMOUNT_ONLY_RE.match(line):
            amount = parse_vn_number(match.group(1))
            if amount < 0:
                full_disc_label = " ".join(pending_discount_text).strip() or "Giảm giá"
                disc_row = Row("discount", full_disc_label, amount)
                discount_rows.append(disc_row)
                last_target = ("discount", disc_row)
                pending_discount_text = []
                pending_item_name = None
                continue
            elif current_qty != "0x":
                if pending_item_name:
                    p_qty, p_name = pending_item_name
                    item_row = Row("item", p_name.strip(), amount)
                    raw_items_with_qty.append((p_qty, item_row))
                    last_target = ("item", item_row)
                    pending_item_name = None
                continue
            else:
                pending_item_name = None
                continue

        # 5. Explicit line with trailing amount: Name/Code amount
        if match := TRAILING_AMOUNT_RE.match(line):
            label_part, raw_amount = match.groups()
            amount = parse_vn_number(raw_amount)
            is_condition_text = any(kw in line.lower() for kw in CONDITION_KEYWORDS)

            if amount < 0:
                # Negative amount is a discount
                full_disc_label = " ".join(pending_discount_text + ([label_part.strip()] if label_part.strip() else [])).strip()
                if not full_disc_label:
                    full_disc_label = "Giảm giá"
                disc_row = Row("discount", full_disc_label, amount)
                discount_rows.append(disc_row)
                last_target = ("discount", disc_row)
                pending_discount_text = []
                pending_item_name = None
                continue
            elif is_condition_text:
                # Condition text inside promo (e.g. "đặt đơn tối thiểu 398.000₫")
                if last_target and last_target[0] == "discount":
                    last_target[1].label = f"{last_target[1].label} {line.strip()}".strip()
                else:
                    pending_discount_text.append(line.strip())
                continue
            elif current_qty != "0x":
                # Positive amount: item name continuation or item without 1x prefix (only if not 0x)
                if pending_item_name:
                    p_qty, p_name = pending_item_name
                    full_name = f"{p_name} {label_part.strip()}".strip()
                    item_row = Row("item", full_name, amount)
                    raw_items_with_qty.append((p_qty, item_row))
                else:
                    item_row = Row("item", label_part.strip(), amount)
                    raw_items_with_qty.append((current_qty, item_row))
                last_target = ("item", item_row)
                pending_item_name = None
                pending_discount_text = []
                continue
            else:
                pending_item_name = None
                continue

        # 6. Discount keyword/promo start line without amount: e.g. "Tặng ngay [Hàng tặng không bán]..."
        if any(hint in line.lower() for hint in DISCOUNT_HINTS):
            pending_discount_text.append(line.strip())
            continue

        # 7. Wrapped text continuation line
        if pending_discount_text:
            pending_discount_text.append(line.strip())
        elif pending_item_name and current_qty != "0x":
            p_qty, p_name = pending_item_name
            pending_item_name = (p_qty, f"{p_name} {line.strip()}".strip())
        elif last_target and current_qty != "0x":
            target_type, target_row = last_target
            target_row.label = f"{target_row.label} {line.strip()}".strip()

    # Group items by quantity dynamically (descending order: 3x, 2x, 1x...)
    qty_groups: dict[str, list[Row]] = {}
    for q, item in raw_items_with_qty:
        if q == "0x" or q == "0":
            continue
        if q not in qty_groups:
            qty_groups[q] = []
        qty_groups[q].append(item)

    def qty_sort_key(q: str) -> int:
        m = re.search(r"\d+", q)
        return int(m.group(0)) if m else 0

    sorted_qtys = sorted(qty_groups.keys(), key=qty_sort_key, reverse=True)

    organized_rows: list[Row] = []
    for q in sorted_qtys:
        if q == "0x" or q == "0" or qty_sort_key(q) <= 0:
            continue
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
    raw_lines = extract_lines(pdf_path)
    parsed = [parse_receipt(part) for part in split_receipts(raw_lines)]
    return [r for r in parsed if r is not None]
