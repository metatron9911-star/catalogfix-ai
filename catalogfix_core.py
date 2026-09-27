import io
import math
import re
import unicodedata
import hashlib
import json
import gzip
from pathlib import Path
from collections import Counter

import pandas as pd
import pdfplumber
import os
import time
from pypdf import PdfReader

RELEASE_VERSION = "1.9.0"
CHECKPOINT_VERSION = "pdf-pipeline-v8"

try:
    import fitz  # PyMuPDF for fast page rendering
except Exception:
    fitz = None

try:
    import numpy as np
except Exception:
    np = None


CANONICAL_FIELDS = [
    "sku", "title", "brand", "price", "category", "size", "color", "description", "barcode"
]

ALIASES = {
    "sku": [
        "sku", "product sku", "item sku", "article", "article no", "art", "code",
        "product code", "item code", "код", "код товара", "код продукта", "код заказа",
        "global product id", "код заказа global product id", "артикул"
    ],
    "title": [
        "title", "product", "product name", "name", "item", "item name", "описание",
        "наименование", "наименование товара", "название", "название товара"
    ],
    "brand": ["brand", "manufacturer", "make", "vendor", "бренд", "производитель", "марка"],
    "price": [
        "price", "cost", "sale price", "unit price", "retail price", "цена", "стоимость",
        "розничная цена", "оптовая цена", "цена за единицу"
    ],
    "category": [
        "category", "product category", "type", "group", "категория", "группа", "раздел",
        "тип товара", "товарная группа"
    ],
    "size": ["size", "product size", "размер"],
    "color": ["color", "colour", "цвет", "цвет товара"],
    "description": ["description", "product description", "details", "body", "подробности"],
    "barcode": ["barcode", "ean", "upc", "gtin", "штрихкод", "штрих код", "штрих-код"],
}

HEADER_LOOKUP = {}
for canonical, aliases in ALIASES.items():
    for alias in aliases:
        HEADER_LOOKUP[alias] = canonical

GLOBAL_PRODUCT_ID_RE = re.compile(r"^[12][A-Z]{2,}[A-Z0-9\-]{7,}$", re.I)


def clean_text(value):
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def norm_header(value):
    value = clean_text(value).lower()
    value = value.replace("ё", "е")
    value = re.sub(r"[\(\)\[\]{}]+", " ", value)
    value = re.sub(r"[_\-–—/:;,.]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def map_columns(columns):
    mapped = {}
    used = set()
    normalized = {column: norm_header(column) for column in columns}
    for canonical, aliases in ALIASES.items():
        alias_norm = {norm_header(a) for a in aliases}
        for column in columns:
            if column in used:
                continue
            if normalized[column] in alias_norm:
                mapped[canonical] = column
                used.add(column)
                break
    return mapped


def clean_brand(value):
    value = clean_text(value)
    if not value:
        return ""
    return " ".join(
        word.upper() if len(word) <= 3 and word.isalpha() else word.capitalize()
        for word in value.split()
    )


def clean_color(value):
    value = clean_text(value)
    if not value:
        return ""
    return " ".join(part.capitalize() for part in value.split())


def clean_price(value):
    if pd.isna(value) or clean_text(value) == "":
        return None
    text = clean_text(value).replace("\u00a0", " ")
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text:
        return None
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    elif "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    try:
        return round(float(text), 2)
    except (TypeError, ValueError):
        return None


def clean_barcode(value):
    if pd.isna(value):
        return ""
    if isinstance(value, bool):
        return clean_text(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if pd.isna(value):
            return ""
        if value.is_integer():
            return str(int(value))
        return format(value, "f").rstrip("0").rstrip(".")
    text = clean_text(value)
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def slugify(value, fallback="product"):
    value = clean_text(value)
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    if slug:
        return slug
    fallback = re.sub(r"[^a-zA-Z0-9]+", "-", clean_text(fallback)).strip("-").lower()
    return fallback or "product"


def _row_values(raw, row_idx):
    vals = []
    for col_idx, value in enumerate(raw.iloc[row_idx].tolist()):
        text = clean_text(value)
        if text:
            vals.append((col_idx, text))
    return vals


def _header_cells(raw, row_idx):
    result = {}
    for col_idx, text in _row_values(raw, row_idx):
        normalized = norm_header(text)
        canonical = HEADER_LOOKUP.get(normalized)
        if canonical and canonical not in result:
            result[canonical] = col_idx
        # Special handling for multi-line ABB-style label.
        if "код заказа" in normalized and "sku" not in result:
            result["sku"] = col_idx
        if "global product id" in normalized and "sku" not in result:
            result["sku"] = col_idx
        if normalized == "описание" and "title" not in result:
            result["title"] = col_idx
    return result


def _is_header_row(raw, row_idx):
    cells = _header_cells(raw, row_idx)
    return len(cells) >= 2 and ("sku" in cells or "title" in cells)


def _looks_like_global_id(value):
    text = clean_text(value).replace(" ", "")
    return bool(GLOBAL_PRODUCT_ID_RE.fullmatch(text))


def _looks_like_compact_code(value):
    text = clean_text(value)
    if not text or len(text) > 45:
        return False
    if _looks_like_global_id(text):
        return False
    if not re.search(r"\d", text):
        return False
    if not re.search(r"[A-Za-z]", text):
        return False
    if len(text.split()) > 6:
        return False
    # Prefer supplier article/type codes rather than sentences.
    if text.count(" ") > 4:
        return False
    return True


def _looks_like_color(value):
    text = clean_text(value)
    if not text or len(text) > 40 or len(text) < 3:
        return False
    if re.search(r"\d", text):
        return False
    if len(text.split()) > 4:
        return False
    if not re.search(r"[A-Za-zА-Яа-яЁё]", text):
        return False
    bad = {"тип", "описание", "код заказа", "цвет", "артикул", "упаковка", "штук", "шт"}
    return norm_header(text) not in bad


def _best_supplier_code(row_items, global_col):
    candidates = []
    for col, text in row_items:
        if not _looks_like_compact_code(text):
            continue
        distance = abs(col - global_col)
        ascii_bonus = 0 if re.search(r"[А-Яа-яЁё]", text) else 4
        slash_bonus = 2 if any(ch in text for ch in "/-") else 0
        score = 30 - distance + ascii_bonus + slash_bonus
        candidates.append((score, -distance, text))
    if not candidates:
        return ""
    return max(candidates)[2]


def _best_color(row_items, global_col):
    candidates = []
    for col, text in row_items:
        if not _looks_like_color(text):
            continue
        # Colors are usually near the article/global-id columns but not long descriptions.
        distance = abs(col - global_col)
        if distance > 35:
            continue
        score = 25 - distance
        if re.search(r"[А-Яа-яЁё]", text):
            score += 3
        candidates.append((score, text))
    return max(candidates)[1] if candidates else ""


def _best_current_description(row_items, excluded):
    candidates = []
    excluded_set = {clean_text(x) for x in excluded if clean_text(x)}
    for col, text in row_items:
        if text in excluded_set:
            continue
        if _looks_like_global_id(text) or _looks_like_compact_code(text):
            continue
        if len(text) < 10 or len(text) > 500:
            continue
        if re.fullmatch(r"[\d\s/.,%-]+", text):
            continue
        score = min(len(text), 250)
        if re.search(r"[А-Яа-яЁёA-Za-z]", text):
            score += 10
        candidates.append((score, text))
    return max(candidates)[1] if candidates else ""


def _nearest_previous_description(raw, row_idx, global_col, lookback=4):
    candidates = []
    for r in range(max(0, row_idx - lookback), row_idx):
        for col, text in _row_values(raw, r):
            if abs(col - global_col) > 25:
                continue
            if len(text) < 15 or len(text) > 700:
                continue
            if _looks_like_global_id(text) or _looks_like_compact_code(text):
                continue
            if _is_header_row(raw, r):
                continue
            score = 120 - (row_idx - r) * 15 - abs(col - global_col)
            score += min(len(text), 200) / 10
            candidates.append((score, text))
    return max(candidates)[1] if candidates else ""


def _infer_brand(raw, filename=""):
    filename_l = (filename or "").lower()
    if "sapeli" in filename_l:
        return "SAPELI"
    if "abb" in filename_l:
        return "ABB"
    abb_hits = 0
    sapeli_hits = 0
    for r in range(min(len(raw), 150)):
        text = " ".join(t for _, t in _row_values(raw, r)).lower()
        abb_hits += len(re.findall(r"\babb\b", text))
        sapeli_hits += len(re.findall(r"\bsapeli\b", text))
    if sapeli_hits >= 2:
        return "SAPELI"
    return "ABB" if abb_hits >= 3 else ""


def _is_section_heading(text):
    text = clean_text(text)
    if not text or len(text) > 120:
        return False
    if _looks_like_global_id(text) or _looks_like_compact_code(text):
        return False
    if re.fullmatch(r"[\d\s/.,%-]+", text):
        return False
    normalized = norm_header(text)
    if normalized in HEADER_LOOKUP:
        return False
    # Converted catalog headings often use em dashes, all caps, or concise noun phrases.
    if text.startswith(("—", "–", "-")):
        return True
    letters = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", text)
    if len(letters) >= 6 and letters.upper() == letters:
        return True
    if len(text.split()) <= 7 and any(k in normalized for k in [
        "выключател", "розет", "механизм", "аксессуар", "шин", "basic", "авдт", "вдт"
    ]):
        return True
    return False


def _row_section_updates(raw):
    sections = {}
    current = ""
    for r in range(len(raw)):
        vals = _row_values(raw, r)
        if len(vals) <= 3 and not any(_looks_like_global_id(text) for _, text in vals):
            joined = " | ".join(text for _, text in vals)
            if _is_section_heading(joined):
                current = joined.lstrip("—–- ")[:120]
        sections[r] = current
    return sections


def extract_header_blocks(raw, sheet_name, brand=""):
    records = []
    header_rows = [r for r in range(len(raw)) if _is_header_row(raw, r)]
    if not header_rows:
        return records

    for pos, header_row in enumerate(header_rows):
        mapping = _header_cells(raw, header_row)
        next_header = header_rows[pos + 1] if pos + 1 < len(header_rows) else len(raw)
        end_row = min(next_header, header_row + 120)

        # A non-canonical cell on the header row is often the table/category name.
        category_hint = ""
        for col, text in _row_values(raw, header_row):
            if col not in mapping.values() and len(text) <= 80:
                category_hint = text
                break

        blank_streak = 0
        for r in range(header_row + 1, end_row):
            vals = _row_values(raw, r)
            if not vals:
                blank_streak += 1
                if blank_streak >= 8:
                    break
                continue
            blank_streak = 0

            rec = {field: "" for field in CANONICAL_FIELDS}
            for field, col_idx in mapping.items():
                if col_idx < raw.shape[1]:
                    rec[field] = clean_text(raw.iat[r, col_idx])

            if "sku" in mapping and not rec.get("sku"):
                continue
            if not rec.get("sku") and not rec.get("title"):
                continue
            # Avoid technical prose lines that accidentally sit under a header.
            if rec.get("sku") and len(rec["sku"]) > 80:
                continue

            rec["brand"] = rec.get("brand") or brand
            rec["category"] = rec.get("category") or category_hint
            rec["source_sheet"] = sheet_name
            rec["source_row"] = r + 1
            rec["supplier_code"] = ""
            rec["import_confidence"] = "HIGH"
            rec["import_method"] = "header-table"
            records.append(rec)
    return records


def extract_pattern_products(raw, sheet_name, filename=""):
    brand = _infer_brand(raw, filename)
    sections = _row_section_updates(raw)
    records = []

    for r in range(len(raw)):
        row_items = _row_values(raw, r)
        for global_col, text in row_items:
            global_id = clean_text(text).replace(" ", "")
            if not GLOBAL_PRODUCT_ID_RE.fullmatch(global_id):
                continue

            supplier_code = _best_supplier_code(row_items, global_col)
            color = _best_color(row_items, global_col)
            current_desc = _best_current_description(row_items, [global_id, supplier_code, color])
            previous_desc = _nearest_previous_description(raw, r, global_col)
            description = current_desc or previous_desc

            if current_desc:
                title = current_desc
                confidence = "HIGH"
            else:
                parts = [brand or "Product", supplier_code or global_id]
                if color:
                    parts.append(color)
                title = " — ".join([p for p in parts if p])
                confidence = "MEDIUM"

            records.append({
                "sku": global_id,
                "title": title,
                "brand": brand,
                "price": "",
                "category": sections.get(r, ""),
                "size": "",
                "color": color,
                "description": description,
                "barcode": "",
                "source_sheet": sheet_name,
                "source_row": r + 1,
                "supplier_code": supplier_code,
                "import_confidence": confidence,
                "import_method": "pattern-id",
            })
    return records


def _dedupe_imported(records):
    columns = CANONICAL_FIELDS + [
        "source_sheet", "source_row", "supplier_code", "import_confidence", "import_method",
        "source_page", "source_table", "matrix_series", "matrix_section", "matrix_model",
        "variant_group", "variant_codes", "currency", "vat_note",
        "attributes_json", "visual_confidence", "router_type",
        "quality_confidence", "quality_flags", "category_source", "dimension_original", "dimension_suggestion"
    ]
    if not records:
        return pd.DataFrame(columns=columns)

    df = pd.DataFrame(records)
    for col in columns:
        if col not in df.columns:
            df[col] = ""

    merged = []
    with_sku = df[df["sku"].map(clean_text).ne("")].copy()
    without_sku = df[df["sku"].map(clean_text).eq("")].copy()

    for sku, group in with_sku.groupby("sku", sort=False):
        # Prefer explicit header-table parsing, then enrich blanks from pattern detection.
        ordered = group.copy()
        ordered["_pref"] = ordered["import_method"].map(lambda x: 0 if x == "header-table" else 1)
        ordered["_title_len"] = ordered["title"].map(lambda x: -len(clean_text(x)))
        ordered = ordered.sort_values(["_pref", "_title_len", "source_row"])
        base = ordered.iloc[0].copy()
        for col in columns:
            if clean_text(base.get(col, "")):
                continue
            for _, candidate in ordered.iterrows():
                value = candidate.get(col, "")
                if clean_text(value):
                    base[col] = value
                    break
        methods = sorted({clean_text(x) for x in group["import_method"] if clean_text(x)})
        if len(methods) > 1:
            base["import_method"] = "+".join(methods)
        merged.append(base[columns].to_dict())

    if not without_sku.empty:
        sig_cols=[c for c in ["title","size","variant_group","source_page"] if c in without_sku.columns]
        if sig_cols:
            tmp=without_sku.copy()
            for c in sig_cols:
                tmp[c]=tmp[c].map(lambda x: clean_text(x).lower())
            keep_idx=tmp.drop_duplicates(subset=sig_cols,keep="first").index
            without_sku=without_sku.loc[keep_idx]
        merged.extend(without_sku[columns].to_dict("records"))

    out = pd.DataFrame(merged, columns=columns)
    if not out.empty:
        out["_row_num"] = pd.to_numeric(out["source_row"], errors="coerce")
        out = out.sort_values(["source_sheet", "_row_num"], na_position="last").drop(columns=["_row_num"])
        out = out.reset_index(drop=True)
    return out


def smart_import_excel(file_obj, filename=""):
    # Read every sheet raw so converted PDFs / merged-cell catalogs can be reconstructed.
    file_obj.seek(0)
    sheets = pd.read_excel(file_obj, sheet_name=None, header=None, dtype=object)
    all_records = []
    sheet_report = []

    for sheet_name, raw in sheets.items():
        raw = raw.dropna(axis=1, how="all")
        if raw.empty:
            continue
        raw = raw.reset_index(drop=True)
        raw.columns = range(raw.shape[1])
        brand = _infer_brand(raw, filename)

        header_records = extract_header_blocks(raw, sheet_name, brand=brand)
        pattern_records = extract_pattern_products(raw, sheet_name, filename=filename)
        records = header_records + pattern_records
        all_records.extend(records)
        sheet_report.append({
            "sheet": sheet_name,
            "source_rows": len(raw),
            "source_columns": raw.shape[1],
            "header_products": len(header_records),
            "pattern_products": len(pattern_records),
        })

    imported = _dedupe_imported(all_records)
    report = pd.DataFrame(sheet_report)
    return imported, report



def _pdf_words_to_raw(page, y_tolerance=3.0, x_gap=18.0):
    """Convert a text PDF page into a rough row/column grid from word coordinates."""
    words = page.extract_words(
        x_tolerance=2,
        y_tolerance=2,
        keep_blank_chars=False,
        use_text_flow=True,
    ) or []
    if not words:
        return pd.DataFrame()

    lines = []
    current = []
    current_top = None
    for word in sorted(words, key=lambda w: (float(w.get("top", 0)), float(w.get("x0", 0)))):
        top = float(word.get("top", 0))
        if current_top is None or abs(top - current_top) <= y_tolerance:
            current.append(word)
            current_top = top if current_top is None else (current_top + top) / 2
        else:
            lines.append(current)
            current = [word]
            current_top = top
    if current:
        lines.append(current)

    rows = []
    for line in lines:
        line = sorted(line, key=lambda w: float(w.get("x0", 0)))
        cells = []
        cell_words = []
        last_x1 = None
        for word in line:
            x0 = float(word.get("x0", 0))
            x1 = float(word.get("x1", x0))
            text = clean_text(word.get("text", ""))
            if not text:
                continue
            if last_x1 is not None and x0 - last_x1 > x_gap and cell_words:
                cells.append(" ".join(cell_words))
                cell_words = []
            cell_words.append(text)
            last_x1 = x1
        if cell_words:
            cells.append(" ".join(cell_words))
        if cells:
            rows.append(cells)

    if not rows:
        return pd.DataFrame()
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    raw = pd.DataFrame(padded, dtype=object)
    raw.columns = range(raw.shape[1])
    return raw



def _pdf_tables_to_raw(page):
    """Prefer native table geometry; use text/line grids only as fallback."""
    def _clean_table(table):
        if not table: return None
        width=max(len(r or []) for r in table)
        if width < 2: return None
        rows=[]
        for row in table:
            cleaned=[clean_text(v) for v in (row or [])]
            rows.append(cleaned+[""]*(width-len(cleaned)))
        raw=pd.DataFrame(rows,dtype=object).dropna(axis=1,how="all")
        if raw.empty: return None
        raw.columns=range(raw.shape[1])
        return raw
    try: native=page.extract_tables() or []
    except Exception: native=[]
    native_raw=[x for x in (_clean_table(t) for t in native) if x is not None]
    if native_raw:
        widths=[r.shape[1] for r in native_raw]
        if max(widths)-min(widths) <= 1:
            target=max(widths); parts=[]
            for r in native_raw:
                rr=r.copy().reindex(columns=range(target),fill_value="")
                parts.append(rr)
            combo=pd.concat(parts,ignore_index=True)
            blob=" ".join(clean_text(x).lower() for row in combo.head(12).values.tolist() for x in row)
            if any(k in blob for k in ["price","mrp","national","retail","assembled"]):
                return [combo]
        native_raw.sort(key=lambda r:r.shape[0]*r.shape[1],reverse=True)
        return native_raw[:20]
    settings_candidates=[
        {"vertical_strategy":"text","horizontal_strategy":"text","snap_tolerance":3,"join_tolerance":3,"text_tolerance":2},
        {"vertical_strategy":"lines","horizontal_strategy":"lines","intersection_tolerance":5},
    ]
    for rank,settings in enumerate(settings_candidates):
        candidates=[]
        try: tables=page.extract_tables(table_settings=settings) or []
        except Exception: tables=[]
        for table in tables:
            raw=_clean_table(table)
            if raw is None or len(raw)<2: continue
            nonempty=sum(bool(clean_text(x)) for row in raw.values.tolist() for x in row)
            candidates.append((nonempty+raw.shape[0]*5+raw.shape[1]*2,raw))
        if candidates:
            candidates.sort(key=lambda x:x[0],reverse=True)
            best_score,best=candidates[0]
            if rank==0 and best.shape[0]>=8 and best.shape[1]>=3: return [best]
            if rank==1: return [raw for score,raw in candidates if score>=best_score*.65][:3]
    return []

def _letters_only(value):
    return re.sub(r"[^a-z]", "", clean_text(value).lower())


def _matrix_section_name(row):
    """Return a compact section label such as Veneer / Colour / CPL laminate."""
    if not row:
        return ""
    # PDF table extractors often split one heading across 2-3 cells.
    lead = " ".join(clean_text(x) for x in row[:4] if clean_text(x))
    if not lead:
        return ""
    lead_letters = _letters_only(lead)
    first_letters = _letters_only(row[0])
    # Avoid variant rows like "Premium (cemented veneers)...".
    if first_letters.startswith("premium") or first_letters.startswith("comfort") or first_letters.startswith("standard"):
        return ""
    canonical = {
        "veneer": "Veneer", "colour": "Colour", "color": "Colour",
        "cpl": "CPL laminate", "hpl": "HPL laminate", "cardboard": "Cardboard",
        "laminate": "Laminate", "foil": "Foil", "paint": "Paint",
        "glass": "Glass", "metal": "Metal", "decor": "Decor",
        "surface": "Surface", "finish": "Finish",
    }
    for key in MATRIX_SECTION_KEYWORDS:
        key_letters = _letters_only(key)
        if first_letters.startswith(key_letters) or lead_letters.startswith(key_letters):
            return canonical.get(key, key.title())
    return ""


def _normalize_model_token(value):
    text = clean_text(value)
    if not text:
        return ""
    # Duplicate text layers can produce "401\\n401".
    parts = [p.strip() for p in re.split(r"[\n\r]+", text) if p.strip()]
    if len(parts) >= 2 and len(set(parts)) == 1:
        text = parts[0]
    text = re.sub(r"\s+", "", text)
    if not re.fullmatch(r"\d{1,8}", text):
        return ""
    # Some SAPELI PDFs overlay each digit twice: 1100 -> 10, 110011 -> 101.
    if len(text) % 2 == 0 and len(text) >= 4:
        pairs = [text[i:i+2] for i in range(0, len(text), 2)]
        if all(len(p) == 2 and p[0] == p[1] for p in pairs):
            collapsed = "".join(p[0] for p in pairs)
            if 1 <= len(collapsed) <= 4:
                text = collapsed
    if len(text) > 4:
        return ""
    try:
        n = int(text)
    except ValueError:
        return ""
    if n <= 0 or n > 9999:
        return ""
    return str(n).zfill(len(text)) if text.startswith("0") else str(n)


def _parse_matrix_price(value):
    text = clean_text(value)
    if not text:
        return None
    # Require a cell that is predominantly numeric, not a prose line containing a surcharge.
    if not re.fullmatch(r"[-+]?\d{1,5}(?:[\s.]\d{3})*(?:[,.]\d{1,2})?", text):
        return None
    return clean_price(text)


def _price_at_model_col(row, col, model_cols):
    if col >= len(row):
        return None
    direct = _parse_matrix_price(row[col])
    # PDF extraction can split 539,7 into adjacent cells "5" + "39,7".
    if direct is not None and col > 0 and (col - 1) not in model_cols:
        prev = clean_text(row[col - 1])
        cur = clean_text(row[col])
        if re.fullmatch(r"\d", prev) and re.fullmatch(r"\d{2},\d", cur):
            return clean_price(prev + cur)
    return direct


def _row_left_text(row, first_model_col):
    cells = []
    for idx, value in enumerate(row):
        if idx >= first_model_col:
            break
        text = clean_text(value)
        if text:
            cells.append(text)
    return clean_text(" ".join(cells))


def _is_matrix_note(text):
    n = norm_header(text)
    if not n:
        return True
    return any(n.startswith(prefix) for prefix in [
        "possibility of finishing", "valid from", "all prices", "www sapeli",
        "contents", "recommended catalogue prices", "door and doorframe prices",
    ])


def _extract_variant_codes(text):
    codes = []
    for token in re.findall(r"\(([A-Za-z0-9][A-Za-z0-9\-]{1,18})\)", clean_text(text)):
        up = token.upper()
        # RAL/NCS colour numbers are attributes, never SKUs.
        if re.fullmatch(r"RAL\d*", up) or re.fullmatch(r"NCS\d*", up):
            continue
        if up not in codes:
            codes.append(up)
    return codes


def _safe_slug_piece(value, max_len=18):
    s = slugify(value, "x").upper().replace("-", "")
    return s[:max_len] or "X"


def _matrix_sku(brand, series, section, model, variant, codes):
    import hashlib
    safe_codes = [c for c in codes if not re.fullmatch(r"(?:RAL|NCS)[- ]?\d+", c, re.I)]
    if len(safe_codes) == 1:
        variant_piece = _safe_slug_piece(safe_codes[0], 14)
    else:
        lead = re.split(r"[:(]", clean_text(variant), maxsplit=1)[0]
        variant_piece = _safe_slug_piece(lead, 14)
    digest = hashlib.sha1(f"{series}|{section}|{model}|{variant}".encode("utf-8")).hexdigest()[:6].upper()
    return f"{_safe_slug_piece(brand or 'CAT',8)}-{_safe_slug_piece(series,16)}-{_safe_slug_piece(model,8)}-{_safe_slug_piece(section,10)}-{variant_piece}-{digest}"


def _detect_series_from_table(raw, fallback=""):
    for r in range(min(len(raw), 12)):
        vals = [clean_text(x) for x in raw.iloc[r].tolist()]
        lead_parts = []
        for value in vals[:8]:
            if not value:
                continue
            if _normalize_model_token(value) or _parse_matrix_price(value) is not None:
                break
            if re.fullmatch(r"[\d\s.,/-]+", value):
                break
            lead_parts.append(value)
        joined = clean_text(" ".join(lead_parts))
        n = norm_header(joined)
        if not joined or n in {"contents", "model prices", "price group"}:
            continue
        if any(k in n for k in ["valid from", "all prices", "www sapeli"]):
            continue
        if _matrix_section_name(vals):
            continue
        if re.search(r"[A-Za-z]", joined) and len(joined) <= 60:
            toks = joined.split()
            if len(toks) >= 3 and all(len(t) <= 3 and t.isalpha() for t in toks[1:]):
                joined = toks[0] + " " + "".join(toks[1:])
            return joined.strip(" |")
    return fallback


def _clean_model_layout_from_row(row):
    layout = {}
    for col, value in enumerate(row):
        model = _normalize_model_token(value)
        if model:
            layout[col] = model
    return layout


def _series_from_page_text(page_text):
    lines = [clean_text(x) for x in (page_text or "").splitlines() if clean_text(x)]
    for i, line in enumerate(lines):
        if "model prices" not in line.lower():
            continue
        for j in range(i - 1, max(-1, i - 7), -1):
            candidate = lines[j]
            n = norm_header(candidate)
            if not candidate or n == "contents" or "price group" in n:
                continue
            if re.fullmatch(r"[\d\s/.,-]+", candidate) or re.search(r"\d", candidate):
                continue
            if "valid from" in n or "www sapeli" in n:
                continue
            if re.search(r"[A-Za-z]", candidate) and len(candidate) <= 70:
                # Drop stray model numbers that sometimes trail the family name.
                candidate = re.sub(r"\s+\d+(?:\s+\d+)*$", "", candidate).strip()
                return candidate
    return ""


def extract_matrix_products(raw, source_name, filename="", page_num=None, table_index=None, series_hint=""):
    """Expand matrix price tables into one record per model × finish × price cell."""
    if raw is None or raw.empty:
        return []
    rows = [[clean_text(x) for x in raw.iloc[r].tolist()] for r in range(len(raw))]
    brand = _infer_brand(raw, filename) or ("SAPELI" if "sapeli" in (filename or "").lower() else "")
    series = clean_text(series_hint) or _detect_series_from_table(raw, fallback=f"Page {page_num}" if page_num else "")

    # Reliable layouts elsewhere on the same table rescue garbled first headers.
    clean_layouts = []
    section_rows = []
    for r, row in enumerate(rows):
        section = _matrix_section_name(row)
        if not section:
            continue
        section_rows.append((r, section))
        layout = _clean_model_layout_from_row(row)
        # A good layout has at least 2 models and they live to the right of the text label.
        if len(layout) >= 2:
            clean_layouts.append((r, layout))

    if not section_rows or not clean_layouts:
        return []

    records = []
    for sec_pos, (header_r, section) in enumerate(section_rows):
        end_r = section_rows[sec_pos + 1][0] if sec_pos + 1 < len(section_rows) else len(rows)
        own_layout = _clean_model_layout_from_row(rows[header_r])
        if len(own_layout) >= 2:
            layout = own_layout
        else:
            # Nearest clean matrix header on this page/table. This specifically repairs
            # duplicated/overlaid model labels in converted catalog PDFs.
            _, layout = min(clean_layouts, key=lambda item: abs(item[0] - header_r))
        model_cols = sorted(layout)
        first_model_col = min(model_cols)
        desc_parts = []
        r = header_r + 1
        while r < end_r:
            row = rows[r]
            left = _row_left_text(row, first_model_col)
            prices = {layout[c]: _price_at_model_col(row, c, set(model_cols)) for c in model_cols}
            prices = {m: p for m, p in prices.items() if p is not None}

            if left and not _is_matrix_note(left):
                # Never treat another heading/footer as a variant description.
                if not _matrix_section_name(row) and not re.search(r"\bMODEL PRICES\b", left, re.I):
                    desc_parts.append(left)

            if prices:
                # Merge continuation price rows when PDF extraction split one logical row.
                merged_prices = dict(prices)
                look = r + 1
                continuation_text = []
                while look < min(end_r, r + 3):
                    next_row = rows[look]
                    next_left = _row_left_text(next_row, first_model_col)
                    next_prices = {layout[c]: _price_at_model_col(next_row, c, set(model_cols)) for c in model_cols}
                    next_prices = {m: p for m, p in next_prices.items() if p is not None}
                    if next_prices and not (set(next_prices) & set(merged_prices)):
                        merged_prices.update(next_prices)
                        if next_left and not _is_matrix_note(next_left):
                            continuation_text.append(next_left)
                        look += 1
                        continue
                    break

                variant = clean_text(" ".join(desc_parts + continuation_text))
                if variant and not _is_matrix_note(variant):
                    # Trim noise that sometimes accumulates before a true variant label.
                    variant = re.sub(r"\s+", " ", variant).strip()
                    codes = _extract_variant_codes(variant)
                    codes_text = ", ".join(codes)
                    supplier_code = codes[0] if len(codes) == 1 else codes_text
                    for model, price in merged_prices.items():
                        sku = _matrix_sku(brand, series, section, model, variant, codes)
                        short_variant = variant if len(variant) <= 140 else variant[:137] + "..."
                        records.append({
                            "sku": sku,
                            "title": f"{series} model {model} — {short_variant}",
                            "brand": brand,
                            "price": price,
                            "category": f"{series} / {section}",
                            "size": model,
                            "color": "",
                            "description": variant,
                            "barcode": "",
                            "source_sheet": source_name,
                            "source_row": r + 1,
                            "supplier_code": supplier_code,
                            "import_confidence": "HIGH" if len(merged_prices) >= 2 else "MEDIUM",
                            "import_method": "matrix-price",
                            "source_page": page_num or "",
                            "source_table": table_index or "",
                            "matrix_series": series,
                            "matrix_section": section,
                            "matrix_model": model,
                            "variant_group": variant,
                            "variant_codes": codes_text,
                            "currency": "EUR" if (brand == "SAPELI" or "eur" in variant.lower()) else "",
                            "vat_note": "VAT exclusive" if brand == "SAPELI" else "",
                        })
                desc_parts = []
                r = max(r + 1, look)
                continue
            r += 1
    return records




PRICE_PAGE_HINTS = (
    "price", "prices", "price/pcs", "surcharge", "extra charge", "catalogue prices",
    "recommended catalogue prices", "doorframe prices", "handles prices", "hardware prices",
    "hinges prices", "locks prices", "eur", "vat exclusive",
)



def _looks_like_price_page(text):
    """Conservative classifier: prose mentioning price/pricing is not a product list."""
    raw=str(text or ""); t=clean_text(raw).lower()
    if not t: return False
    money=re.findall(r"(?:[$€£₽]|\b(?:usd|eur|gbp|pln|uah|zar|inr|aud|cad)\b|(?<![A-Za-z])r(?=\s?\d))\s*\d[\d .,'’]*",raw,re.I)
    strong=bool(re.search(r"\b(?:price\s*list|pricelist|price\s*book|price\s*guide|pricing\s*schedule|list\s*price|retail\s*price|unit\s*price|mrp|assembled\s*price|kit\s*price)\b",t,re.I))
    table_price=bool(re.search(r"\b(?:list\s*price|retail\s*price|unit\s*price|mrp|assembled\s*price|kit\s*price|price/uom|base\s*price)\b",t,re.I))
    code_header=bool(re.search(r"\b(?:item|part|stock|product|support|model|cat(?:alog)?)[ #._-]*(?:no|number|code|#)\b",t,re.I))
    if money and (strong or len(money)>=2): return True
    if table_price and code_header:
        endings=re.findall(r"(?:^|\n).{2,180}?\s\d{1,7}(?:[,.]\d{1,4})?\s*$",raw,re.M)
        return len(endings)>=2
    return False

def _generic_page_series(page_text, page_num=None):
    lines = [clean_text(x) for x in (page_text or "").splitlines() if clean_text(x)]
    bad = ("valid from", "all prices", "www.", "contents")
    for line in lines[:14]:
        n = line.lower()
        if any(x in n for x in bad):
            continue
        # Strip printed page counters like "| 71".
        line = re.sub(r"\s*\|\s*\d+\s*$", "", line).strip()
        if 3 <= len(line) <= 90 and re.search(r"[A-Za-zА-Яа-я]", line):
            return line
    return f"Page {page_num}" if page_num else "Catalog"


def _dimension_token(value):
    t = clean_text(value).replace("–", "-").replace("—", "-")
    if not t:
        return ""
    if re.fullmatch(r"\d{1,3}\s*-\s*\d{1,3}", t):
        return re.sub(r"\s+", "", t)
    if re.fullmatch(r"(?:\d{1,3}\s*,\s*)+\d{1,3}(?:/\d{2,3})?", t):
        return re.sub(r"\s+", "", t)
    if re.fullmatch(r"\d{1,3}(?:/\d{2,3})?", t):
        return t
    return ""


def _column_heading(rows, row_idx, col, max_lookback=12):
    parts = []
    for rr in range(max(0, row_idx-max_lookback), row_idx):
        if col >= len(rows[rr]):
            continue
        v = clean_text(rows[rr][col])
        if not v or _parse_matrix_price(v) is not None or _dimension_token(v):
            continue
        nv = norm_header(v)
        if nv in {"price", "price pcs", "dimension cm", "wall thick cm", "rebated", "without semi rail"}:
            continue
        if v not in parts:
            parts.append(v)
    return clean_text(" ".join(parts[-4:]))


def extract_dimension_matrix_products(raw, source_name, filename="", page_num=None, table_index=None, series_hint=""):
    """Expand dimension × option × price tables (doorframes, technical matrices, etc.)."""
    if raw is None or raw.empty:
        return []
    rows = [[clean_text(x) for x in raw.iloc[r].tolist()] for r in range(len(raw))]
    brand = _infer_brand(raw, filename) or ("SAPELI" if "sapeli" in (filename or "").lower() else "")
    series = clean_text(series_hint) or _detect_series_from_table(raw, fallback=f"Page {page_num}" if page_num else "Catalog")
    records = []
    current_section = ""
    current_dimension_group = ""

    for r, row in enumerate(rows):
        sec = _matrix_section_name(row)
        if sec:
            current_section = sec

        # Detect a standalone dimension-group row such as "60, 70, 80, 90/197".
        nonempty = [clean_text(x) for x in row if clean_text(x)]
        if len(nonempty) <= 3:
            for x in nonempty:
                dt = _dimension_token(x)
                if dt and ("," in dt or "/" in dt):
                    current_dimension_group = dt

        price_cells = []
        for c, val in enumerate(row):
            p = _parse_matrix_price(val)
            if p is not None:
                price_cells.append((c, p))
        if not price_cells:
            continue

        # Need at least one dimension/range before first price, otherwise this is a normal row-price table.
        first_price_col = min(c for c, _ in price_cells)
        left = row[:first_price_col]
        dims = [_dimension_token(v) for v in left]
        dims = [d for d in dims if d]
        if not dims:
            continue
        # Avoid treating model-price matrices as dimensions; those are handled by extract_matrix_products.
        if len(price_cells) >= 2 and all(re.fullmatch(r"\d{1,4}", d or "") for d in dims):
            continue

        wall = dims[-1] if dims else ""
        dim = current_dimension_group or (dims[0] if len(dims) > 1 else "")
        if len(dims) >= 2:
            dim = dims[0] if ("," in dims[0] or "/" in dims[0]) else current_dimension_group
            wall = dims[-1]
        if not dim and current_dimension_group:
            dim = current_dimension_group

        section = current_section or "Price matrix"
        for c, price in price_cells:
            heading = _column_heading(rows, r, c) or f"Option {c-first_price_col+1}"
            # Filter headers that are just page furniture.
            if any(x in norm_header(heading) for x in ["valid from", "all prices", "www sapeli", "contents"]):
                heading = f"Option {c-first_price_col+1}"
            variant = clean_text(f"{heading}; dimension {dim}; wall thickness {wall}")
            codes = _extract_variant_codes(heading)
            sku = _matrix_sku(brand or "CAT", series, section, dim or wall or str(r+1), variant, codes)
            records.append({
                "sku": sku,
                "title": f"{series} — {section} — {dim or wall} — {heading}",
                "brand": brand,
                "price": price,
                "category": f"{series} / {section}",
                "size": clean_text(f"{dim} | wall {wall}" if dim and wall else (dim or wall)),
                "color": "",
                "description": variant,
                "barcode": "",
                "source_sheet": source_name,
                "source_row": r + 1,
                "supplier_code": ", ".join(codes),
                "import_confidence": "MEDIUM",
                "import_method": "dimension-matrix",
                "source_page": page_num or "",
                "source_table": table_index or "",
                "matrix_series": series,
                "matrix_section": section,
                "matrix_model": dim or wall,
                "variant_group": heading,
                "variant_codes": ", ".join(codes),
                "currency": "EUR" if (brand == "SAPELI" or "eur" in variant.lower()) else "",
                "vat_note": "VAT exclusive" if brand == "SAPELI" else "",
            })
    return records



def extract_tabular_price_products(raw, source_name, filename="", page_num=None, table_index=None, series_hint=""):
    """Parse ordinary price tables, including two side-by-side tables on one PDF page."""
    if raw is None or raw.empty:
        return []
    rows = [[clean_text(x) for x in raw.iloc[r].tolist()] for r in range(len(raw))]
    brand = _infer_brand(raw, filename) or ("SAPELI" if "sapeli" in (filename or "").lower() else "")
    series = clean_text(series_hint) or _detect_series_from_table(raw, fallback=f"Page {page_num}" if page_num else "Catalog")
    out = []

    for hr, header in enumerate(rows):
        price_cols = [c for c, v in enumerate(header) if norm_header(v) in {"price", "price pcs", "price pc", "price piece", "price pieces"} or norm_header(v).startswith("price ")]
        if not price_cols:
            continue
        prev_pc = -1
        for pc in price_cols:
            start = prev_pc + 1
            prev_pc = pc
            labels = {c: norm_header(header[c]) for c in range(start, pc + 1)}
            text_cols = [c for c in range(start, pc) if clean_text(header[c])]
            if not text_cols:
                text_cols = list(range(start, pc))
            name_col = text_cols[0] if text_cols else start
            surface_col = next((c for c in range(start, pc) if "surface" in labels.get(c, "") or "finish" in labels.get(c, "")), None)
            variant_cols = [c for c in range(start, pc) if c not in {name_col, surface_col}]
            last_name = ""
            last_surface = ""
            blank_run = 0
            for rr in range(hr + 1, len(rows)):
                row = rows[rr]
                # Stop at a new table header/major heading.
                if rr != hr + 1 and any(norm_header(v) in {"price", "price pcs", "price pc"} for v in row):
                    break
                price = _parse_matrix_price(row[pc] if pc < len(row) else "")
                if price is None:
                    if not any(clean_text(x) for x in row[start:pc+1]):
                        blank_run += 1
                        if blank_run >= 4:
                            break
                    continue
                blank_run = 0
                name = clean_text(row[name_col]) if name_col < len(row) else ""
                surface = clean_text(row[surface_col]) if surface_col is not None and surface_col < len(row) else ""
                if name:
                    last_name = name
                else:
                    name = last_name
                if surface:
                    last_surface = surface
                else:
                    surface = last_surface
                variants = []
                for c in variant_cols:
                    if c < len(row):
                        v = clean_text(row[c])
                        if v:
                            variants.append(v)
                if not name or norm_header(name) in {"handles", "safety hardware", "locks", "hinges"}:
                    continue
                title = clean_text(" ".join([name, surface] + variants))
                if not title or len(title) > 180:
                    continue
                codes = _extract_variant_codes(title)
                sku = _matrix_sku(brand or "CAT", series, "Row price", str(rr + 1), title, codes)
                out.append({
                    "sku": sku,
                    "title": title,
                    "brand": brand,
                    "price": price,
                    "category": series,
                    "size": "",
                    "color": "",
                    "description": title,
                    "barcode": "",
                    "source_sheet": source_name,
                    "source_row": rr + 1,
                    "supplier_code": ", ".join(codes),
                    "import_confidence": "HIGH",
                    "import_method": "table-row-price",
                    "source_page": page_num or "",
                    "source_table": table_index or "",
                    "matrix_series": series,
                    "matrix_section": "Row price",
                    "matrix_model": "",
                    "variant_group": title,
                    "variant_codes": ", ".join(codes),
                    "currency": "EUR" if brand == "SAPELI" else "",
                    "vat_note": "VAT exclusive" if brand == "SAPELI" else "",
                })
    return out


def extract_row_price_products_from_text(page_text, source_name, filename="", page_num=None, series_hint=""):
    """Generic row-price parser for hardware/accessories lists where each line ends in a price."""
    brand = "SAPELI" if "sapeli" in (filename or "").lower() else ""
    series = clean_text(series_hint) or _generic_page_series(page_text, page_num)
    lines = [clean_text(x) for x in (page_text or "").splitlines() if clean_text(x)]
    records = []
    previous_base = ""
    for idx, line in enumerate(lines, start=1):
        low = line.lower()
        if any(x in low for x in ["valid from", "all prices are", "www.", "contents"]):
            continue
        m = re.match(r"^(.*?)([-+]?\d{1,5}(?:[,.]\d{1,2})?)\s*$", line)
        if not m:
            continue
        left = clean_text(m.group(1))
        price = clean_price(m.group(2))
        if price is None or not left:
            continue
        if len(left) < 2 or re.fullmatch(r"[\d\s,./()\-+]+", left):
            continue
        # Exclude prose surcharge sentences; dedicated surcharge parser can handle those later.
        if len(left) > 150 or any(x in low for x in ["to price of", "otherwise +", "included in price", "possibility of finishing"]):
            continue
        # Continuation rows (WC/PZ/etc.) inherit the previous product base.
        if re.fullmatch(r"(?:WC|BB|PZ|O|P|L|R|left|right)(?:\s+.*)?", left, re.I) and previous_base:
            title_left = f"{previous_base} {left}"
        else:
            title_left = left
            previous_base = left
        codes = _extract_variant_codes(title_left)
        sku = _matrix_sku(brand or "CAT", series, "Row price", str(idx), title_left, codes)
        records.append({
            "sku": sku,
            "title": title_left,
            "brand": brand,
            "price": price,
            "category": series,
            "size": "",
            "color": "",
            "description": title_left,
            "barcode": "",
            "source_sheet": source_name,
            "source_row": idx,
            "supplier_code": ", ".join(codes),
            "import_confidence": "MEDIUM",
            "import_method": "row-price",
            "source_page": page_num or "",
            "source_table": "text",
            "matrix_series": series,
            "matrix_section": "Row price",
            "matrix_model": "",
            "variant_group": title_left,
            "variant_codes": ", ".join(codes),
            "currency": "EUR" if (brand == "SAPELI" or "eur" in low) else "",
            "vat_note": "VAT exclusive" if brand == "SAPELI" else "",
        })
    return records



# -----------------------------
# v1.5 Universal Import Router / Visual Catalog Engine
# -----------------------------
_VISUAL_CODE_PATTERNS = [
    re.compile(r"\bB\d{4,6}\b", re.I),
    # Explicit separator: tolerate OCR spacing around '-'/'_' and allow one digit.
    re.compile(r"\b[A-Z]{1,5}\s*[-_]\s*\d{1,5}[A-Z]?\b", re.I),
    # Whitespace-only separator remains stricter: at least two digits.
    re.compile(r"\b[A-Z]{1,5}\s+\d{2,5}[A-Z]?\b", re.I),
    # Compact codes remain stricter too: at least two digits.
    re.compile(r"\b[A-Z]{2,5}\d{2,5}[A-Z]?\b", re.I),
]
_FALSE_CODE_PREFIXES = {
    "RAL", "NCS", "EUR", "USD", "VAT", "CM", "MM", "KG", "PCS", "PDF", "PAGE",
    "WWW", "HTTP", "RGB", "PANTONE", "ISO", "DIN",
    # Common prose/header prefixes that otherwise resemble whitespace SKU forms.
    "THE", "ITEM", "FIG",
}
MATRIX_SECTION_KEYWORDS = (
    "veneer", "colour", "color", "cpl", "hpl", "cardboard",
    "laminate", "foil", "paint", "glass", "metal", "decor",
    "surface", "finish",
)

_TECH_DOC_HINTS = (
    "absolute maximum ratings", "electrical characteristics", "typical characteristics",
    "pin configuration", "pin functions", "package option addendum", "package materials information",
    "package outline", "example board layout", "example stencil design", "revision history",
    "thermal information", "recommended operating conditions", "application information",
    "mechanical, packaging, and orderable information",
    "datasheet", "data sheet", "technical data", "operating manual", "installation guide",
    "wiring diagram", "schematic", "compliance", "material safety", "test report", "engineering",
    "绝对最大额定值", "电气特性", "典型特性", "引脚配置", "引脚功能", "封装热阻",
    "修订历史记录", "机械、封装和可订购信息", "封装和可订购信息",
)

_COMMERCIAL_PRICE_HINTS = (
    "price list", "pricelist", "unit price", "retail price", "list price",
    "wholesale price", "catalogue price", "recommended catalogue price", "sale price",
    "order form", "quotation", "quote", "mrp", "price/pcs", "surcharge",
    "kit price", "assembled price", "base price", "price/uom",
    "розничная цена", "оптовая цена",
)

_STATISTICAL_REPORT_HINTS = (
    "statistical", "average price", "median price", "percentile",
    "price and purity", "price index", "market report",
    "street purity", "wholesale purity", "drug group", "drug type", "purity type",
)

_GENERIC_VISUAL_HEADINGS = {
    "contents", "collection", "professional beauty", "tools", "beauty", "product",
    "products", "new", "style", "styles", "grand award", "personal care"
}

def _clean_visual_code(value):
    raw = clean_text(value).upper().replace("_", "-")
    text = re.sub(r"\s+", "", raw)
    # Supplier codes are usually PREFIX + digits. One-digit codes are accepted only
    # when an explicit '-'/'_' separator survives normalization (e.g. MWO-1).
    # Whitespace-only one-digit forms such as "FIG 3" remain rejected.
    m = re.match(r"^([A-Z]{1,5})(-?)(\d{1,6}[A-Z]?)$", text)
    if m:
        prefix, sep, digits = m.groups()
        numeric_part = re.match(r"(\d+)", digits).group(1)
        if len(numeric_part) == 1 and not sep:
            return ""
        if prefix in _FALSE_CODE_PREFIXES:
            return ""
        # Common OCR confusion in visual catalogs: zero is read as the letter O,
        # e.g. B0104 -> BO104, B0009 -> BOO09.
        if re.fullmatch(r"B[O0]{1,3}", prefix) and re.fullmatch(r"\d{1,5}[A-Z]?", digits):
            zeros = len(prefix) - 1
            core = ("0" * zeros) + digits
            numeric = re.match(r"(\d+)([A-Z]?)$", core)
            if numeric:
                nums, suffix = numeric.groups()
                nums = nums[-4:].zfill(4)
                return "B" + nums + suffix
        # Single-letter prefixes are too noisy except the very common B#### family.
        if len(prefix) == 1 and prefix != "B":
            return ""
        # B0104 style is normally printed without a hyphen.
        if prefix == "B" and len(digits) >= 4:
            return prefix + digits
        return f"{prefix}-{digits}" if len(prefix) <= 3 else prefix + digits
    return ""

def _visual_codes_from_text(text):
    found = []
    raw = clean_text(text).upper()
    if not raw:
        return found
    for pattern in _VISUAL_CODE_PATTERNS:
        for m in pattern.finditer(raw):
            code = _clean_visual_code(m.group(0))
            if code and code not in found:
                # avoid likely years or dimensions accidentally prefixed by OCR garbage
                if re.fullmatch(r"[A-Z]-?(19|20)\d{2}", code):
                    continue
                found.append(code)
    return found

def _get_ocr_engine():
    """Return ('rapidocr3', engine), legacy RapidOCR, Tesseract, or (None, None).

    RapidOCR's maintained package is now ``rapidocr``.  The older
    ``rapidocr-onnxruntime`` package does not support Python 3.14, so we prefer
    the maintained package and keep the old import only as a compatibility
    fallback for existing installations.
    """
    try:
        from rapidocr import RapidOCR
        if not hasattr(_get_ocr_engine, "_rapid3"):
            _get_ocr_engine._rapid3 = RapidOCR()
        return "rapidocr3", _get_ocr_engine._rapid3
    except Exception:
        pass
    try:
        from rapidocr_onnxruntime import RapidOCR
        if not hasattr(_get_ocr_engine, "_rapid_legacy"):
            _get_ocr_engine._rapid_legacy = RapidOCR()
        return "rapidocr_legacy", _get_ocr_engine._rapid_legacy
    except Exception:
        pass
    try:
        import pytesseract
        # This also verifies that the executable is discoverable.
        _ = pytesseract.get_tesseract_version()
        return "tesseract", pytesseract
    except Exception:
        return None, None

def _render_pdf_page_for_ocr(doc, page_num, dpi=150):
    if fitz is None or np is None:
        raise RuntimeError("Visual PDF parsing needs PyMuPDF and numpy.")
    page = doc.load_page(page_num - 1)
    zoom = max(1.0, float(dpi) / 72.0)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        arr = arr[:, :, :3]
    return arr

def _ocr_boxes(image):
    engine_name, engine = _get_ocr_engine()
    if not engine_name:
        raise RuntimeError(
            "Visual/image-only catalog detected, but no OCR engine is available. "
            "Run START_WINDOWS_CMD again so RapidOCR can be installed."
        )
    boxes = []
    if engine_name == "rapidocr3":
        result = engine(image)
        # RapidOCR >= 2 returns RapidOCROutput with boxes/txts/scores.
        polys = getattr(result, "boxes", None)
        txts = getattr(result, "txts", None)
        scores = getattr(result, "scores", None)
        if polys is None or txts is None or scores is None:
            return boxes, engine_name
        for poly, text, score in zip(polys, txts, scores):
            try:
                text = clean_text(text)
                score = float(score)
                if not text:
                    continue
                xs = [float(p[0]) for p in poly]
                ys = [float(p[1]) for p in poly]
                boxes.append({"text": text, "score": score, "bbox": (min(xs), min(ys), max(xs), max(ys))})
            except Exception:
                continue
        return boxes, engine_name

    if engine_name == "rapidocr_legacy":
        result, _ = engine(image)
        for item in result or []:
            try:
                poly, text, score = item[0], clean_text(item[1]), float(item[2])
                if not text:
                    continue
                xs = [float(p[0]) for p in poly]
                ys = [float(p[1]) for p in poly]
                boxes.append({"text": text, "score": score, "bbox": (min(xs), min(ys), max(xs), max(ys))})
            except Exception:
                continue
        return boxes, engine_name

    # Tesseract fallback for development machines / users who already have it installed.
    from PIL import Image
    output = engine.image_to_data(Image.fromarray(image), output_type=engine.Output.DICT, config="--psm 11")
    n = len(output.get("text", []))
    for i in range(n):
        text = clean_text(output["text"][i])
        try:
            conf = float(output["conf"][i]) / 100.0
        except Exception:
            conf = 0.0
        if not text or conf < 0.18:
            continue
        x, y, w, h = (float(output[k][i]) for k in ("left", "top", "width", "height"))
        boxes.append({"text": text, "score": max(0.0, min(conf, 1.0)), "bbox": (x, y, x+w, y+h)})
    return boxes, engine_name

def _bbox_center(b):
    x1, y1, x2, y2 = b
    return ((x1+x2)/2.0, (y1+y2)/2.0)

_VISUAL_CATEGORY_PATTERNS = [
    (r"bath\s+sponge", "Bath Sponge"),
    (r"bath\s+body\s+brush|natural\s+bath\s+body\s+brush", "Bath Body Brush"),
    (r"eyelash\s+curler", "Eyelash Curler"),
    (r"foundation\s+brush", "Foundation Brush"),
    (r"eye\s+mask", "Eye Mask"),
    (r"hair\s*clip", "Hair Clip"),
    (r"head\s*band", "Headband"),
    (r"hair\s*band", "Hair Band"),
    (r"hair\s*pin", "Hairpin"),
    (r"grab\s+clamp|claw\s+clip", "Grab Clamp"),
    (r"massage\s+hair\s+comb", "Massage Hair Comb"),
    (r"telescopic\s+brush", "Telescopic Brush"),
    (r"makeup\s+brush", "Makeup Brush"),
    (r"nail\s+file", "Nail File"),
    (r"manicure|nail\s+clipper", "Manicure Tool"),
    (r"comb", "Comb"),
    (r"hair\s+brush", "Hair Brush"),
]

_MARKETING_WORDS = re.compile(
    r"\b(comfortable|perfect|beautiful|professional|quality|design|feel|your|you|our|best|new|"
    r"fashion|fashionable|experience|enjoy|ideal|suitable|easy|convenient|special)\b", re.I
)

def _looks_like_marketing_copy(text):
    t = clean_text(text)
    if not t:
        return True
    words = re.findall(r"[A-Za-z]+", t)
    if len(words) >= 9:
        return True
    if len(words) >= 5 and _MARKETING_WORDS.search(t):
        return True
    if re.search(r"[.!?]$", t) and len(words) >= 5:
        return True
    return False


def _looks_like_visual_heading(text):
    t = clean_text(text)
    if len(t) < 3 or len(t) > 70:
        return False
    low = t.lower()
    if low in _GENERIC_VISUAL_HEADINGS or _looks_like_marketing_copy(t):
        return False
    if _visual_codes_from_text(t):
        return False
    if not re.search(r"[A-Za-z]", t):
        return False
    if re.search(r"https?://|www\.", low):
        return False
    return True


def _category_from_text(text):
    low = clean_text(text).lower()
    for pattern, label in _VISUAL_CATEGORY_PATTERNS:
        if re.search(pattern, low, re.I):
            return label
    return ""


def _visual_heading(boxes, image_shape):
    # Page-wide category is only a fallback. Product-level association below is preferred.
    joined = " ".join(clean_text(b.get("text", "")) for b in boxes)
    return _category_from_text(joined) or "Visual Catalog"


def _box_distance(a, b, image_shape):
    ax, ay = _bbox_center(a["bbox"]); bx, by = _bbox_center(b["bbox"])
    h, w = image_shape[:2]
    dx = abs(ax-bx) / max(w, 1)
    dy = abs(ay-by) / max(h, 1)
    # Vertical proximity matters more in product cards.
    return dx + 1.45*dy


def _local_visual_context(code_box, boxes, image_shape):
    """Return clean nearby label/category using page geometry, not just OCR order."""
    cx, cy = _bbox_center(code_box["bbox"])
    h, w = image_shape[:2]
    candidates = []
    for b in boxes:
        if b is code_box:
            continue
        text = clean_text(b.get("text", ""))
        if not _looks_like_visual_heading(text) or float(b.get("score", 0) or 0) < 0.50:
            continue
        tx, ty = _bbox_center(b["bbox"])
        # Product labels are normally in the same card: within 30% page width / 18% height.
        if abs(tx-cx) > 0.30*w or abs(ty-cy) > 0.18*h:
            continue
        dist = _box_distance(code_box, b, image_shape)
        # Prefer text above the SKU or on the same line; text far below is often copy/body text.
        positional_penalty = 0.0 if ty <= cy + 0.025*h else 0.12
        category = _category_from_text(text)
        category_bonus = -0.22 if category else 0.0
        score = dist + positional_penalty + category_bonus - 0.08*float(b.get("score",0) or 0)
        candidates.append((score, text, category))
    candidates.sort(key=lambda x: x[0])
    if not candidates:
        return "", ""
    best = candidates[0]
    return best[1], best[2]


def _ocr_visual_pass(doc, page_num, dpi):
    image = _render_pdf_page_for_ocr(doc, page_num, dpi=dpi)
    boxes, engine_name = _ocr_boxes(image)
    return image, boxes, engine_name


_SIZE_ANCHOR_RE = re.compile(
    r"(?:size\s*[:：]?\s*)?(\d{2,4})\s*[x×*]\s*(\d{1,4})(?:\s*[x×*]\s*(\d{1,4}))?\s*(mm|cm)?\b",
    re.I,
)

def _size_from_visual_text(text):
    """Normalize a visual dimension label without treating it as a supplier SKU."""
    t = clean_text(text).replace("＊", "*").replace("×", "x")
    m = _SIZE_ANCHOR_RE.search(t)
    if not m:
        return ""
    nums = [x for x in m.groups()[:3] if x]
    unit = (m.group(4) or "mm").lower()
    # Reject implausible OCR fragments; dimensions are only anchors, never SKUs.
    vals = [int(x) for x in nums]
    if not vals or any(v <= 0 or v > 5000 for v in vals):
        return ""
    return "x".join(str(v) for v in vals) + unit

def _visual_card_fallback(page_num, boxes, image_shape, existing_records, page_heading):
    """Create review-only product candidates from repeated dimension/card anchors.

    Many image-only supplier catalogues contain one product photo per size label but no
    printed SKU at all.  v1.6.1 must not invent a supplier code.  Instead it creates a
    stable local candidate id (VIS-P....), preserves the dimension and page coordinates,
    and marks the record LOW/MEDIUM confidence for human review.
    """
    existing = []
    for r in existing_records:
        try:
            attrs=json.loads(r.get("attributes_json") or "{}")
            bb=attrs.get("sku_bbox")
            if bb and len(bb)==4: existing.append(tuple(float(x) for x in bb))
        except Exception:
            pass
    anchors=[]
    for b in boxes:
        size=_size_from_visual_text(b.get("text", ""))
        if not size or float(b.get("score",0) or 0) < 0.42:
            continue
        cx,cy=_bbox_center(b["bbox"])
        # If a true supplier SKU is already very close, the normal path owns this card.
        near_code=False
        for bb in existing:
            ex,ey=_bbox_center(bb)
            h,w=image_shape[:2]
            if abs(ex-cx) < .14*w and abs(ey-cy) < .12*h:
                near_code=True; break
        if not near_code:
            anchors.append((cy,cx,b,size))
    anchors.sort()
    out=[]
    seen=[]
    h,w=image_shape[:2]
    for _,_,b,size in anchors:
        cx,cy=_bbox_center(b["bbox"])
        # Deduplicate the same label seen twice by OCR passes / overlapping fragments.
        if any(abs(cx-x)<.025*w and abs(cy-y)<.025*h and size==sz for x,y,sz in seen):
            continue
        seen.append((cx,cy,size))
        idx=len(out)+1
        local_id=f"VIS-P{page_num:04d}-{idx:02d}"
        category=page_heading if page_heading and page_heading!="Visual Catalog" else "Visual Catalog"
        title=(f"{category} {size}" if category!="Visual Catalog" else f"Visual item {size}")
        conf=max(.45,min(.79,float(b.get("score",0) or 0)))
        x1,y1,x2,y2=b["bbox"]
        out.append({
            "sku": local_id, "title": title, "brand": "", "price": None,
            "category": category, "size": size, "color": "",
            "description": f"Visual product candidate detected from a repeated size/card anchor on PDF page {page_num}. Supplier SKU was not printed or not confidently detected.",
            "barcode": "", "source_sheet": f"PDF p.{page_num}", "source_row": page_num,
            "supplier_code": "", "import_confidence": "MEDIUM" if conf>=.62 else "LOW",
            "import_method": "visual-card-fallback", "source_page": page_num,
            "source_table": "visual-card-segmentation", "matrix_series": category,
            "matrix_section": "Visual catalog fallback", "matrix_model": "",
            "variant_group": size, "variant_codes": "", "currency": "", "vat_note": "",
            "visual_confidence": round(conf,3), "router_type": "VISUAL_ADAPTIVE",
            "attributes_json": json.dumps({
                "page_heading": page_heading, "card_anchor": clean_text(b.get("text","")),
                "size": size, "supplier_sku_missing": True, "generated_candidate_id": local_id,
                "anchor_bbox": [round(x1,1),round(y1,1),round(x2,1),round(y2,1)],
                "scanner": "v1.6.1-adaptive-visual", "review_required": True
            }, ensure_ascii=False),
        })
    return out



def _visual_price_value(text):
    t=clean_text(text).replace("₹","").replace("/-","").strip()
    if not t:
        return None
    m=re.search(r"(?:[$€£₹₽]|\b(?:USD|EUR|GBP|INR|ZAR|RM|R)\b)?\s*([0-9][0-9 .,'’]{0,18}[0-9]|[0-9])",t,re.I)
    if not m:
        return None
    raw=re.sub(r"[\s'’]","",m.group(1))
    if "," in raw and "." in raw:
        # Right-most separator is decimal; the other is grouping.
        if raw.rfind(",") > raw.rfind("."):
            raw=raw.replace(".","").replace(",",".")
        else:
            raw=raw.replace(",","")
    elif "," in raw:
        parts=raw.split(",")
        if len(parts)>2 or (len(parts)==2 and len(parts[-1])==3):
            raw="".join(parts)
        else:
            raw=raw.replace(",",".")
    elif "." in raw:
        parts=raw.split(".")
        # Single . with exactly three trailing digits is usually a thousands separator.
        if len(parts)>2 or (len(parts)==2 and len(parts[-1])==3 and len(parts[0])>=1):
            raw="".join(parts)
    try:
        v=float(raw)
        return v if 0 < v < 1e9 else None
    except Exception:
        return None

def _visual_price_candidates(boxes, image_shape):
    """
    Return exactly the price candidates consumed by
    _visual_named_price_fallback().

    Production and debug tracing share this helper so observation cannot
    drift from production candidate detection.
    """
    if not boxes:
        return []

    h,w=image_shape[:2]

    def xy(b):
        return _bbox_center(b["bbox"])

    def price_value(b):
        text=clean_text(b.get("text",""))
        val=_visual_price_value(text)
        if val is None:
            return None
        if re.search(r"(?:[$€£₹₽]|/-|\b(?:USD|EUR|GBP|INR|ZAR|RM)\b)", text, re.I):
            return val
        bx,by=xy(b)
        for q in boxes:
            qx,qy=xy(q)
            if re.fullmatch(r"(?:list\s*)?price[:：]?", clean_text(q.get("text","")), re.I) and abs(qy-by)<.045*h and 0 <= bx-qx < .18*w:
                return val
        return None

    prices=[]
    for b in boxes:
        val=price_value(b)
        if val is not None and float(b.get("score",0) or 0)>=.30:
            bx,by=xy(b)
            prices.append((by,bx,b,val))
    if not prices:
        return []
    prices.sort()

    compact=[]
    for by,bx,b,val in prices:
        if any(abs(by-y)<.018*h and abs(bx-x)<.035*w and abs(val-v)<.01 for y,x,_,v in compact):
            continue
        compact.append((by,bx,b,val))
    return compact


def _visual_has_price_gate(boxes):
    """Exact predicate used by production OCR-pass escalation logic."""
    return any(
        _visual_price_value(b.get("text","")) is not None
        and re.search(r"(?:[$€£₹₽]|/-)", clean_text(b.get("text","")))
        for b in boxes
    )


# --- B4 visual association geometry ---
VISUAL_ASSOC_LANE_BOUNDARY_V1 = 0.50
VISUAL_ASSOC_MAX_BLOCK_HEIGHT_V1 = 0.445


def _visual_lane(cx):
    """cx is normalized (0..1). cx < 0.50 -> LEFT, cx >= 0.50 -> RIGHT."""
    return "LEFT" if cx < VISUAL_ASSOC_LANE_BOUNDARY_V1 else "RIGHT"


def _visual_owner(candidate_cy, skus_in_lane):
    """Return the owning SKU code for a normalized candidate center-y, or None.

    skus_in_lane must already be filtered to one lane and sorted ascending by
    normalized cy. Ownership is top-down with half-open intervals:
    [sku.cy, next_sku.cy) for non-last SKUs, and
    [sku.cy, sku.cy + VISUAL_ASSOC_MAX_BLOCK_HEIGHT_V1) for the last SKU.
    """
    if not skus_in_lane:
        return None

    for index, sku in enumerate(skus_in_lane):
        start = float(sku["cy"])
        if index + 1 < len(skus_in_lane):
            end = float(skus_in_lane[index + 1]["cy"])
        else:
            end = start + VISUAL_ASSOC_MAX_BLOCK_HEIGHT_V1
        if start <= candidate_cy < end:
            return sku["code"]
    return None


# B4-v1 empirical association eligibility. Intentionally narrow and derived
# only from the observed AppliancesPriceList harness format. This is not a
# universal price parser; association wiring is introduced in a later commit.
_B4_V1_PRICE_ACCEPT_RE = re.compile(
    r"^\s*\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?\s*/-\s*$"
)
_B4_V1_DIMENSION_RE = re.compile(r"\d+\s*[xX×]\s*\d+")
_B4_V1_DIMENSION_UNIT_RE = re.compile(r"\d(?:[\d.,]*\s*)?(?:mm|cm)\b", re.I)
_B4_V1_MALFORMED_APOSTROPHE_RE = re.compile(r"\d\s*['’]\s*\d")


def _visual_association_price_classification_v1(raw_text):
    """Classify one OCR token for B4-v1 association eligibility.

    Reject rules win before accept. Acceptance is a full-string match of the
    observed western-grouped INR-style '/-' form. Indian lakh grouping is a
    known v1 exclusion and therefore falls into OTHER_REJECT.
    """
    text = clean_text(raw_text)
    if _B4_V1_DIMENSION_RE.search(text) or _B4_V1_DIMENSION_UNIT_RE.search(text):
        return "DIMENSION_REJECT"
    if "-/" in text or _B4_V1_MALFORMED_APOSTROPHE_RE.search(text):
        return "MALFORMED_REJECT"
    if _B4_V1_PRICE_ACCEPT_RE.fullmatch(text):
        return "ELIGIBLE"
    return "OTHER_REJECT"


def _visual_named_price_fallback(page_num, boxes, image_shape, existing_records, filename="", page_heading=""):
    """Review-only recovery for image-only catalogues with product name + visible price."""
    if not boxes:
        return []
    h,w=image_shape[:2]
    if any(clean_text(r.get("supplier_code","")) for r in existing_records):
        return []

    def xy(b):
        return _bbox_center(b["bbox"])

    prices=_visual_price_candidates(boxes, image_shape)
    if not prices:
        return []

    known_bad=re.compile(
        r"\b(material|width|control|speed|motor|lighting|filter|outlet|model|size|power|airflow|sensor|heat|type|suction|remote|stainless|glass|touch|gesture|yes|no|aluminium|aluminum|copper|collector|body|panel|voltage|frequency|capacity|dimensions?|watt|rpm|duct|finish|colour|color|warranty|input|output)\b",
        re.I
    )
    category_words=re.compile(r"\b(hood|hob|induction|cooktop|cooking range|refrigerator|fridge|freezer|chiller|microwave|oven|dishwasher|washer|dryer|appliance)\b",re.I)

    joined=" ".join(clean_text(b.get("text","")) for b in boxes)
    brand=""
    for known in ("CARYSIL","ORIFLAME","FIAT","IMSAI","ROCA"):
        if known.lower() in joined.lower() or known.lower() in (filename or "").lower():
            brand=known.title()
            break

    cat=page_heading if page_heading and page_heading!="Visual Catalog" else ""
    if not cat:
        cc=[]
        for b in boxes:
            text=clean_text(b.get("text",""))
            if category_words.search(text) and len(text)<=35:
                _,cy=xy(b)
                cc.append((cy,text))
        if cc:
            cc.sort()
            cat=cc[0][1]
    cat=cat or "Visual Catalog"

    out=[]
    used=set()
    for by,bx,pb,price in prices:
        side=0 if bx < .5*w else 1
        prev_y=-1
        for oy,ox,_,_ in prices:
            if oy>=by:
                break
            if (0 if ox<.5*w else 1)==side and abs(ox-bx)<.25*w:
                prev_y=max(prev_y,oy)
        top=max(0,prev_y+.018*h)
        left=0 if side==0 else .48*w
        right=.55*w if side==0 else w

        candidates=[]
        for b in boxes:
            text=clean_text(b.get("text","")).strip(" .:;-")
            if not text:
                continue
            tx,ty=xy(b)
            if not (left<=tx<=right and top<ty<by-.035*h):
                continue
            if len(text)<3 or len(text)>48 or known_bad.search(text):
                continue
            if re.fullmatch(r"(?:price|carysil|ceiling hood|island hood|built-in hood|smart hoods?)",text,re.I):
                continue
            if re.search(r"(?:[$€£₹₽]|/-)",text) or re.fullmatch(r"[\d\s.,x×*/%+-]+",text):
                continue
            if _visual_codes_from_text(text):
                continue
            words=re.findall(r"[A-Za-z][A-Za-z'&.-]*",text)
            if not words or len(words)>6 or len("".join(words))<4:
                continue
            score=(ty-top)/max(h,1)+.18*abs(tx-(left+.10*w))/max(w,1)+.02*len(words)
            if category_words.search(text):
                score+=.22
            candidates.append((score,ty,text,b))
        if not candidates:
            continue
        candidates.sort(key=lambda z:(z[0],z[1]))
        title=candidates[0][2]
        key=(title.lower(),round(price,2),page_num)
        if key in used:
            continue
        used.add(key)

        import hashlib
        local_id="CAND-"+hashlib.sha1(f"{page_num}|{title}|{price}".encode("utf-8")).hexdigest()[:12].upper()
        out.append({
            "sku":local_id,"title":title,"brand":brand,"price":price,"category":cat,"size":"","color":"",
            "description":title,"barcode":"","source_sheet":f"PDF p.{page_num}","source_row":page_num,"supplier_code":"",
            "import_confidence":"MEDIUM","import_method":"visual-named-price-card","source_page":page_num,
            "source_table":"visual-price-card","matrix_series":cat,"matrix_section":"Image-only price card","matrix_model":"",
            "variant_group":title,"variant_codes":"","currency":"INR" if ("₹" in joined or "/-" in clean_text(pb.get("text",""))) else "",
            "vat_note":"","visual_confidence":round(float(pb.get("score",0) or .5),3),"router_type":"VISUAL_ADAPTIVE",
            "quality_flags":"supplier_sku_missing","category_source":"visual-card",
            "attributes_json":json.dumps({
                "supplier_sku_missing":True,"generated_candidate_id":local_id,"ocr_price":price,
                "review_required":True,"price_bbox":[round(x,1) for x in pb["bbox"]],
                "scanner":f"v{RELEASE_VERSION}-image-card"
            },ensure_ascii=False)
        })
    return out


def extract_visual_catalog_products(doc, page_num, filename="", dpi=150):
    """Adaptive visual OCR without retaining full raster images between passes."""
    passes=[]
    image,boxes,engine_name=_ocr_visual_pass(doc,page_num,dpi)
    shape=image.shape
    passes.append((dpi,shape,boxes,engine_name))
    del image

    first_codes=sum(len(_visual_codes_from_text(b.get("text",""))) for b in boxes)
    avg_score=(sum(float(b.get("score",0) or 0) for b in boxes)/len(boxes)) if boxes else 0.0
    has_price=_visual_has_price_gate(boxes)
    if first_codes==0 or avg_score<0.72 or not has_price:
        hi_dpi=max(220,int(dpi*1.45))
        try:
            image2,boxes2,engine2=_ocr_visual_pass(doc,page_num,hi_dpi)
            shape2=image2.shape
            passes.append((hi_dpi,shape2,boxes2,engine2))
            del image2
        except Exception:
            pass

    hits={}
    for pass_dpi,pshape,pboxes,eng in passes:
        for b in pboxes:
            for code in _visual_codes_from_text(b.get("text","")):
                item=(float(b.get("score",0) or 0),b,pboxes,pshape,pass_dpi,eng)
                if code not in hits or item[0]>hits[code][0]:
                    hits[code]=item

    best=max(passes,key=lambda p:len(p[2])) if passes else (dpi,(1,1,3),[],engine_name)
    page_boxes=best[2]
    page_shape=best[1]
    page_heading=_visual_heading(page_boxes,page_shape)

    brand=""
    stem=Path(filename or "").stem
    if stem and stem.lower() not in {"1","catalog","catalogue","price","pricelist"}:
        possible=re.sub(r"[_-]+"," ",stem).strip()
        if 2<=len(possible)<=40 and re.search(r"[A-Za-z]",possible):
            brand=possible

    records=[]
    for code,(ocr_conf,code_box,pboxes,pshape,pass_dpi,eng) in sorted(hits.items()):
        nearby_label,local_category=_local_visual_context(code_box,pboxes,pshape)
        category=local_category or page_heading or "Visual Catalog"
        if category=="Visual Catalog" and nearby_label and not _looks_like_marketing_copy(nearby_label):
            title=f"{nearby_label} {code}" if len(nearby_label.split())<=6 else code
        else:
            title=f"{category} {code}" if category!="Visual Catalog" else code
        layout_bonus=.10 if local_category else .05 if nearby_label else 0.0
        scanner_conf=min(.99,max(0.0,ocr_conf+layout_bonus))
        confidence="HIGH" if scanner_conf>=.88 else "MEDIUM" if scanner_conf>=.62 else "LOW"
        x1,y1,x2,y2=code_box["bbox"]
        records.append({
            "sku":code,"title":title,"brand":brand,"price":None,"category":category,"size":"","color":"",
            "description":f"Visual catalog item detected on PDF page {page_num}.","barcode":"",
            "source_sheet":f"PDF p.{page_num}","source_row":page_num,"supplier_code":code,"import_confidence":confidence,
            "import_method":"visual-high-intelligence","source_page":page_num,"source_table":"visual-layout-page",
            "matrix_series":category,"matrix_section":"Visual catalog","matrix_model":code,"variant_group":nearby_label or category,
            "variant_codes":code,"currency":"","vat_note":"","visual_confidence":round(scanner_conf,3),"router_type":"VISUAL_HI",
            "attributes_json":json.dumps({
                "page_heading":page_heading,"nearby_label":nearby_label,"local_category":local_category,
                "ocr_engine":eng,"ocr_dpi":pass_dpi,"ocr_confidence":round(ocr_conf,3),
                "scanner_confidence":round(scanner_conf,3),
                "sku_bbox":[round(x1,1),round(y1,1),round(x2,1),round(y2,1)],
                "price_source":"missing","scanner":f"v{RELEASE_VERSION}-adaptive-high-intelligence"
            },ensure_ascii=False)
        })

    card_records=_visual_card_fallback(page_num,page_boxes,page_shape,records,page_heading)
    records.extend(card_records)
    named_price_records=_visual_named_price_fallback(
        page_num,page_boxes,page_shape,records,filename=filename,page_heading=page_heading
    )
    records.extend(named_price_records)

    report={
        "sheet":f"PDF p.{page_num}","source_rows":sum(len(p[2]) for p in passes),"source_columns":0,
        "matrix_products":0,"dimension_products":0,"row_price_products":0,"header_products":0,"pattern_products":0,
        "visual_products":len(records),"router_type":"VISUAL_ADAPTIVE",
        "scan_status":f"adaptive-{engine_name}-passes{len(passes)}-ocrboxes{len(page_boxes)}-cards{len(card_records)}-named{len(named_price_records)}"
    }
    passes.clear()
    return records,report

def _document_type_safety_v18(page_texts):
    """Return a conservative document classification from fast extracted text."""
    texts=[clean_text(x) for x in (page_texts or []) if clean_text(x)]
    if not texts:
        return {"type":"unknown","technical_hits":0,"commercial_hits":0}
    joined="\n".join(texts).lower()
    tech_hits=sum(1 for h in _TECH_DOC_HINTS if h.lower() in joined)
    commercial_hits=sum(1 for h in _COMMERCIAL_PRICE_HINTS if h.lower() in joined)
    explicit_money = bool(re.search(
        r"(?:[$€₽]|\b(?:usd|eur|gbp|pln|uah)\b)\s*\d|\d\s*(?:[$€₽]|\b(?:usd|eur|gbp|pln|uah)\b)",
        joined, re.I
    ))
    commercial_evidence = commercial_hits + (2 if explicit_money else 0)
    stat_hits=sum(1 for h in _STATISTICAL_REPORT_HINTS if h.lower() in joined)
    if stat_hits >= 4 and "price and purity" in joined:
        return {"type":"statistical-report","technical_hits":tech_hits,"commercial_hits":commercial_hits,"statistical_hits":stat_hits}
    # Generic legal phrases such as "purchase price" must not turn a datasheet into a catalogue.
    if tech_hits >= 4 and commercial_evidence == 0:
        return {"type":"technical-datasheet","technical_hits":tech_hits,"commercial_hits":commercial_hits}
    return {"type":"catalog-or-unknown","technical_hits":tech_hits,"commercial_hits":commercial_hits,"statistical_hits":stat_hits}



def _page_has_commercial_signal_v19(text):
    """Conservative second-stage probe for hybrid datasheet+catalog PDFs."""
    raw=str(text or "")
    low=raw.lower()
    if not low.strip():
        return False

    strong_phrases=(
        "price list","pricelist","order form","unit price","retail price","list price",
        "kit price","assembled price","price/uom","quotation","recommended catalogue price"
    )
    if any(p in low for p in strong_phrases):
        return True

    code_header=bool(re.search(r"\b(?:item|part|stock|product|support)\s*(?:no|number|code|#)\b", low, re.I))
    price_header=bool(re.search(r"\b(?:unit|list|retail|assembled|kit|base)\s+price\b", low, re.I))
    if code_header and price_header:
        return True

    money_tokens=re.findall(
        r"(?:[$€£₽₹]\s*\d[\d ,.]*|\d[\d ,.]*\s*(?:USD|EUR|GBP|PLN|UAH|INR|ZAR|AUD|CAD)\b)",
        raw,re.I
    )
    commercial_context=bool(re.search(r"\b(?:order|catalog(?:ue)?|product|item|sku|qty|quantity|discount)\b", low,re.I))
    # One stray currency value in a legal footnote is not enough.
    return len(money_tokens) >= 2 and commercial_context

def classify_pdf_page_text(text):
    """Cheap routing decision used before heavy parsing."""
    t = clean_text(text)
    if len(t) < 25:
        return "VISUAL"
    if _has_numeric_product_card_signal(text):
        return "TEXT_PRODUCT"
    if _looks_like_price_page(t):
        return "STRUCTURED_PRICE"
    # Text-rich product catalogs can still contain SKUs without prices.
    if len(_visual_codes_from_text(t)) >= 2:
        return "TEXT_PRODUCT"
    return "TEXT_OTHER"

def _checkpoint_job_id(data, filename=""):
    """Stable ID for resume/autosave of the exact PDF bytes, even if the file is renamed."""
    h = hashlib.sha256()
    h.update(str(len(data)).encode("ascii"))
    # Hash the full file for correctness. Streamlit already holds uploaded bytes in memory.
    h.update(data)
    return h.hexdigest()[:16]


def _load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _replace_with_retry(tmp, path, attempts=8):
    """Windows-safe replace: antivirus/indexers can briefly lock freshly written files."""
    import os
    import time
    last_error = None
    for attempt in range(attempts):
        try:
            os.replace(str(tmp), str(path))
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.08 * (attempt + 1))
    # Fallback: losing atomicity is better than aborting a multi-thousand-page job.
    try:
        data = Path(tmp).read_bytes()
        with open(path, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        try:
            Path(tmp).unlink()
        except OSError:
            pass
        return
    except Exception:
        if last_error is not None:
            raise last_error
        raise


def _save_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name prevents collisions with a stale/locked manifest.json.tmp on Windows.
    tmp = path.parent / f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _replace_with_retry(tmp, path)


def _save_gzip_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, default=str)
    _replace_with_retry(tmp, path)


def _load_gzip_json(path, default=None):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default




# -----------------------------
# v1.7.6 Title Recovery + Multi-card Split + QA Gate
# -----------------------------
_NUMERIC_SKU_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")
_CARD_MARKER_RE = re.compile(r"^[\s!\"#№]*\d{5}\b|^[\s!\"#№]{1,5}[A-Za-zА-Яа-яЁё]", re.I)
_PROMO_PHRASES = (
    "специальная цена", "при заказе", "при покупке", "предложение действительно",
    "подробная информация", "подробности на", "подробнее на стр", "выполнение условий",
    "ограничение продажи", "всего за", "скидка", "эксклюзивное предложение",
    "закажи сейчас", "любой продукт", "любая тушь", "баллов бонуса", "до конца каталога",
)
_TITLE_STOP_PREFIXES = (
    "не является", "способ применения", "применение", "принимай", "нанеси", "используй",
    "по результатам", "макияж модели", "зарядись", "запомни", "скидка", "закажи",
    "материал", "размер", "состав", "ингредиенты", "объем", "объём",
)


def _catalog_text_normalize(text):
    t = str(text or "")
    t = re.sub(r"/uni[0-9A-Fa-f]{4}", " ", t)
    t = t.replace("\u00a0", " ").replace("−", "-").replace("–", "-").replace("—", "-")
    return re.sub(r"[ \t]+", " ", t)


def _has_numeric_product_card_signal(text):
    t = _catalog_text_normalize(text)
    if re.search(r"(?:^|\n)\s*[!\"#№]{1,4}\s*\d{5}\s+[A-Za-zА-Яа-яЁё]", t, re.M):
        return True
    marked_title = re.search(r"(?:^|\n)\s*[!\"#№]{1,4}\s*[A-Za-zА-Яа-яЁё]", t, re.M)
    has_unit_price = bool(re.search(r"\b\d+(?:[,.]\d+)?\s*(?:мл|г\.?|ml|g)\b", t, re.I)) and bool(re.search(r"\b\d{2,5}\s*[pрr₽]\b", t, re.I))
    return bool(marked_title and has_unit_price and len(_NUMERIC_SKU_RE.findall(t)) >= 1)


def _clean_card_title(text):
    t = _catalog_text_normalize(text)
    t = re.sub(r"^[\s!\"#№]+", "", t)
    t = re.sub(r"^\d{5}\s+", "", t)
    # PDF column ordering can inject a standalone promo word into a wrapped product title.
    t = re.sub(r"\bпредложение\b", " ", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" .:-")
    return t


def _money_values(text):
    t = _catalog_text_normalize(text)
    vals=[]
    for m in re.finditer(r"(?<!\d)(\d{2,5})(?:[,.]\d{1,2})?\s*[pрr₽](?![A-Za-zА-Яа-я])", t, re.I):
        try: vals.append(float(m.group(1)))
        except Exception: pass
    return vals


def _choose_catalog_price(text):
    """Choose the normal/current catalogue price while ignoring conditional promo tails.
    For a regular/old + current pair, the second lower value is selected.
    """
    safe=[]
    for line in _catalog_text_normalize(text).splitlines():
        low=clean_text(line).lower()
        if any(x in low for x in _PROMO_PHRASES):
            break
        safe.append(line)
    safe_text="\n".join(safe)
    vals=_money_values(safe_text)
    if not vals:
        return None
    # Some PDF layouts split the currency glyph away from the current price and interleave
    # a variant code between them. Recover a standalone 2-4 digit lower price conservatively.
    old=vals[0]
    for line in safe:
        q=clean_text(line)
        if re.search(r"\b(?:ББ|BB)\b", q, re.I):
            break
        mstand=re.search(r"(?:^|\s)(\d{2,4})$", q)
        if mstand:
            cand=float(mstand.group(1))
            if 20 <= cand < old:
                return cand
    if len(vals)>=2 and vals[1] <= vals[0]:
        return vals[1]
    if len(vals)>=2 and vals[-1] < vals[0] and len(vals)<=3:
        return vals[-1]
    return vals[0]


def _size_from_text(text):
    t=_catalog_text_normalize(text)
    m=re.search(r"\b(\d+(?:[,.]\d+)?)\s*(мл|г\.?|ml|g)\b", t, re.I)
    return (m.group(1).replace(",", ".")+" "+m.group(2).rstrip(".")) if m else ""


def _is_variant_label(text):
    t=clean_text(text)
    if not t or len(t)>70 or len(t.split())>8:
        return False
    if len(t)<2 or re.fullmatch(r"[pрr₽]+", t, re.I):
        return False
    if re.search(r"!ОТЕЛ|\bОТЭ\b|ИНМОПАЗ", t, re.I):
        return False
    low=t.lower()
    if any(x in low for x in _PROMO_PHRASES) or any(low.startswith(x) for x in _TITLE_STOP_PREFIXES):
        return False
    if re.search(r"\b(?:код|заказ|каталог|страниц|пробник|сумм|акци|партнер|продуктов|ограничение|часов|недель|месяц)\b", low):
        return False
    if _money_values(t) or _size_from_text(t):
        return False
    if not re.search(r"[A-Za-zА-Яа-яЁё]", t):
        return False
    if t.endswith((".", ";", ":")) or len(re.findall(r"[,;:]", t))>1:
        return False
    return True


def _shade_pairs(text):
    """Extract 5-digit variant code/name pairs from the PDF text layer without inventing SKUs.

    Handles vertical code/name lists, inline pairs and horizontal rows where several codes are
    followed by the same number of short labels. Mirrored/reversed PDF artefacts lose to a
    stronger forward occurrence.
    """
    lines=[clean_text(x) for x in _catalog_text_normalize(text).splitlines() if clean_text(x)]
    candidates=[]

    # Horizontal matrix: ``43295 43296 ...`` then ``Кремовый Нюд Пепельная Роза ...``.
    for i,line in enumerate(lines[:-1]):
        ms=list(_NUMERIC_SKU_RE.finditer(line))
        if len(ms)>=2 and not re.search(r"[A-Za-zА-Яа-яЁё]", line):
            nxt=lines[i+1]
            if not _NUMERIC_SKU_RE.search(nxt) and _is_variant_label(nxt):
                words=nxt.split()
                n=len(ms)
                if len(words)%n==0 and 1 <= len(words)//n <= 4:
                    step=len(words)//n
                    for k,m in enumerate(ms):
                        label=" ".join(words[k*step:(k+1)*step])
                        if _is_variant_label(label):
                            candidates.append((m.group(1),label,6.0,i))

    for i,line in enumerate(lines):
        matches=list(_NUMERIC_SKU_RE.finditer(line))
        if not matches:
            continue
        for k,m in enumerate(matches):
            code=m.group(1)
            seg_end=matches[k+1].start() if k+1<len(matches) else len(line)
            tail=clean_text(line[m.end():seg_end])
            inline=bool(tail)
            if not tail and len(matches)==1:
                parts=[]
                for j in range(i+1, min(len(lines), i+4)):
                    nxt=lines[j]
                    if _NUMERIC_SKU_RE.search(nxt):
                        break
                    if any(x in nxt.lower() for x in _PROMO_PHRASES):
                        break
                    if re.fullmatch(r"[pрr₽]+", nxt, re.I):
                        continue
                    # A size can share the line with the shade: ``9 мл. Горячий Шоколад``.
                    nxt2=re.sub(r"^\s*\d+(?:[,.]\d+)?\s*(?:мл|г\.?|ml|g)\s*", "", nxt, flags=re.I).strip(" .:-")
                    if _money_values(nxt2):
                        break
                    # Interleaved description + shade: keep the compact suffix after a sentence boundary.
                    if "." in nxt2:
                        suffix=clean_text(nxt2.rsplit(".",1)[-1]).strip(" .:-")
                        if _is_variant_label(suffix): nxt2=suffix
                    if _is_variant_label(nxt2):
                        parts.append(nxt2)
                        break
                    elif _size_from_text(nxt) and not nxt2:
                        continue
                    else: break
                tail=clean_text(" ".join(parts))
            tail=re.split(r"\s+(?:\*?По результатам|Абсолютный комфорт|Зарядись|ЗАПОМНИ|ЛЕТНИЙ|СКИДКА|ЗАКАЖИ)\b", tail, maxsplit=1, flags=re.I)[0].strip(" .:-")
            if not _is_variant_label(tail):
                continue
            score=5.0 if inline else 3.5
            score += 1.0 if len(tail.split())<=3 else 0.0
            score += 0.5 if len(tail)<=32 else 0.0
            candidates.append((code,tail,score,i))

    best={}; order=[]
    for code,tail,score,i in candidates:
        if code not in best:
            order.append(code); best[code]=(tail,score,i)
        elif score>best[code][1]:
            best[code]=(tail,score,i)

    # Mirrored PDF objects often yield reversed numeric codes (e.g. 73773 beside 37737).
    # If both exist, keep the stronger text-layer pairing; on ties prefer the one seen inline.
    removed=set()
    for code in list(order):
        rev=code[::-1]
        if rev!=code and rev in best and code in best:
            a=best[code]; b=best[rev]
            if a[1] < b[1]: removed.add(code)
            elif b[1] < a[1]: removed.add(rev)
            else:
                # Same score: keep the occurrence appearing earlier only when its label is compact;
                # otherwise retain the later counterpart.
                removed.add(code if a[2] > b[2] else rev)
    return [(code,best[code][0]) for code in order if code not in removed]

def _family_marker_text(line):
    m=re.search(r"!!\s*(.+)$", line)
    if not m: return ""
    t=m.group(1).strip()
    if re.match(r"^\d{5}\b", t): return ""
    # Column interleaving can append a variant code to the title line.
    t=re.sub(r"\s+\d{5}\s*$", "", t).strip()
    return t


def _title_fragment(extra):
    x=clean_text(extra)
    if not x: return "", False
    low=x.lower()
    if any(y in low for y in _PROMO_PHRASES):
        # Standalone injected word 'предложение' is column noise; skip but keep reading.
        if low.strip()=="предложение": return "", False
        return "", True
    if "%" in x:
        if "***" in x:
            suffix=clean_text(x.split("***",1)[1])
            if suffix and len(suffix.split())<=5: return suffix, False
        return "", True
    if re.search(r"\b(?:ББ|BB)\b", x, re.I): return "", True
    # Keep text before an inline size/price marker, then stop.
    cut=len(x)
    m1=re.search(r"\b\d+(?:[,.]\d+)?\s*(?:мл|г\.?|ml|g)\b", x, re.I)
    m2=re.search(r"(?<!\d)\d{2,5}(?:[,.]\d{1,2})?\s*[pрr₽]", x, re.I)
    if m1: cut=min(cut,m1.start())
    if m2: cut=min(cut,m2.start())
    frag=clean_text(x[:cut])
    stop=(cut<len(x))
    # Any additional catalogue SKU on a wrapped line is a hard card boundary.
    if any(low.startswith(y) for y in _TITLE_STOP_PREFIXES) or _NUMERIC_SKU_RE.search(x): return "", True
    if frag.startswith("•") or len(frag)>95: return "", True
    if frag.endswith("."): stop=True
    return frag, stop


def _family_title_from_lines(lines, i):
    first=_family_marker_text(lines[i]) or _clean_card_title(lines[i])
    parts=[_clean_card_title(first)] if first else []
    for extra in lines[i+1:i+7]:
        frag,stop=_title_fragment(extra)
        if frag:
            current=clean_text(" ".join(parts)).lower()
            fw=frag.split()[0].lower() if frag.split() else ""
            if fw and current.endswith(fw):
                break
            parts.append(frag)
        if stop: break
    title=_clean_card_title(" ".join(parts))
    title=re.split(r"\b(?:Зарядись|ЗАПОМНИ|СКИДКА|ЗАКАЖИ|Подробнее)\b", title, maxsplit=1, flags=re.I)[0].strip(" .:-")
    return title

def extract_product_card_products_from_text(page_text, source_name, filename="", page_num=None):
    """v1.7.6 conservative product-card parser using the PDF text layer as SKU authority."""
    raw=_catalog_text_normalize(page_text)
    lines=[clean_text(x) for x in raw.splitlines() if clean_text(x)]
    if not lines: return []
    brand = "Oriflame" if "oriflame" in raw.lower() or re.search(r"\bTHE ONE\b|\bNovage\+?\b|\bEclat\b", raw, re.I) else ""
    records=[]

    # Explicit article cards: marker + 5-digit article + title.
    starts=[]
    for i,line in enumerate(lines):
        m=re.search(r'[!\"#№]{1,4}\s*(\d{5})\s+(.+)$', line)
        if m and re.search(r"[A-Za-zА-Яа-яЁё]", m.group(2)):
            starts.append((i,m.group(1),m.group(2)))
    for pos,(i,sku,first_title) in enumerate(starts):
        next_start=starts[pos+1][0] if pos+1<len(starts) else len(lines)
        end=min(next_start, i+18)
        block=[]
        for j in range(i,end):
            low=lines[j].lower()
            if j>i and any(x in low for x in _PROMO_PHRASES): break
            block.append(lines[j])
        btxt="\n".join(block)
        title_parts=[first_title]
        for extra in block[1:7]:
            frag,stop=_title_fragment(extra)
            if frag: title_parts.append(frag)
            if stop: break
        title=_clean_card_title(" ".join(title_parts))
        price=_choose_catalog_price(btxt); size=_size_from_text(btxt)
        if not title or price is None or len(title)<4: continue
        records.append({
            "sku":sku,"title":title,"brand":brand,"price":price,"category":"","size":size,"color":"",
            "description":title,"barcode":"","source_sheet":source_name,"source_row":i+1,"supplier_code":sku,
            "import_confidence":"HIGH","import_method":"product-card","source_page":page_num or "","source_table":"text-card",
            "matrix_series":"","matrix_section":"Product card","matrix_model":"","variant_group":"","variant_codes":"",
            "currency":"RUB" if re.search(r"[pрr₽]", btxt, re.I) else "","vat_note":"",
            "quality_confidence":0.96,"quality_flags":"","category_source":"product-card"
        })

    # Family cards: no base article on title; shade/article list may be before OR after the title.
    marked=[i for i,line in enumerate(lines) if _family_marker_text(line)]
    for mpos,i in enumerate(marked):
        next_mark=marked[mpos+1] if mpos+1<len(marked) else len(lines)
        forward_end=min(next_mark, i+55)
        block=[]
        for j in range(i,forward_end):
            low=lines[j].lower()
            if j>i+3 and any(x in low for x in _PROMO_PHRASES): break
            block.append(lines[j])
        btxt="\n".join(block)
        title=_family_title_from_lines(lines,i)
        size=_size_from_text(btxt)
        price=_choose_catalog_price(btxt)
        if not title or price is None:
            continue

        # Find a bounded backwards window, but never cross another product marker or hard promo boundary.
        back=max(0,i-32)
        for j in range(i-1, back-1, -1):
            low=lines[j].lower()
            if '!!' in lines[j] or any(x in low for x in _PROMO_PHRASES):
                back=j+1; break
        # Variant swatches may legally sit on either side of a promo banner on the same page.
        # Scan the bounded local page window and let _shade_pairs reject promo/legal lines itself.
        variant_text="\n".join(lines[back: min(len(lines), i+55)])
        variants=_shade_pairs(variant_text)
        if not variants:
            continue
        existing={r["sku"] for r in records}
        for sku,shade in variants:
            if sku in existing: continue
            records.append({
                "sku":sku,"title":title,"brand":brand,"price":price,"category":"","size":size,"color":shade,
                "description":title,"barcode":"","source_sheet":source_name,"source_row":i+1,"supplier_code":sku,
                "import_confidence":"HIGH","import_method":"product-card-variant","source_page":page_num or "","source_table":"text-card",
                "matrix_series":"","matrix_section":"Product card","matrix_model":"","variant_group":title,"variant_codes":sku,
                "currency":"RUB" if re.search(r"[pрr₽]", btxt, re.I) else "","vat_note":"",
                "quality_confidence":0.98,"quality_flags":"","category_source":"product-card"
            })
            existing.add(sku)

    # Shared-current-price layout: adjacent explicit cards can each show the regular price,
    # followed by one common current price (before a conditional special-price block).
    explicit=[r for r in records if r.get("import_method")=="product-card"]
    if len(explicit)>=2 and starts:
        last_start=starts[-1][0]
        shared=_choose_catalog_price("\n".join(lines[last_start:min(len(lines), last_start+22)]))
        if shared is not None:
            for r in explicit:
                try:
                    if float(r.get("price") or 0) > float(shared): r["price"]=shared
                except Exception:
                    pass

    return records


# v1.8.15 Order-form / dual-price catalogue parser

_BAD_ITEM_WORDS_V181 = {"price","total","note","see","page","item","kit","qty","quantity","terms","handling","tax"}


_BAD_ITEM_WORDS_V181 = {
    "price","total","note","see","page","item","item no","item number","kit",
    "qty","quantity","terms","handling","tax","description","assembled"
}

def _looks_like_item_code_v181(value):
    t=clean_text(value)
    if not t or len(t)>24:
        return False
    low=norm_header(t)
    if low in _BAD_ITEM_WORDS_V181:
        return False
    if re.search(r"\b(?:price|description|qty|quantity|total|handling|tax|terms|note|page|see)\b", t, re.I):
        return False
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ./\-]{0,22}", t):
        return False
    if len(t.split()) > 4:
        return False
    if re.search(r"\d", t):
        return True
    # Legacy catalogues can use genuine all-letter supplier codes (FIF, FDC, LIF, PSM, RM).
    compact=re.sub(r"[^A-Za-z]","",t)
    return bool(re.fullmatch(r"[A-Z]{2,8}", compact))

def extract_order_form_products_v181(page, page_text, source_name, filename="", page_num=None):
    """Parse catalogue/order-form pages with ITEM NO + DESCRIPTION + KIT/ASSEMBLED prices.

    Uses word coordinates to avoid PDF text-layer column interleaving. When both kit
    and assembled prices exist, the assembled price is exported and both source prices
    are retained in attributes_json for auditability.
    """
    t=clean_text(page_text)
    low=t.lower()
    strong_header=("item no" in low and "description" in low and ("assembled" in low or "kit" in low) and "price" in low)
    legacy_order_form=("item no" in low and "description" in low and "order form" in low and len(re.findall(r"\$\s*\d", page_text or "")) >= 6)
    if not (strong_header or legacy_order_form):
        return []
    try:
        words=page.extract_words(x_tolerance=1.5, y_tolerance=2.5, keep_blank_chars=False)
    except Exception:
        return []
    if not words:
        return []
    width=float(page.width or 612.0)
    # Find the table header to calibrate columns.
    header_words=[w for w in words if str(w.get("text","")).lower().startswith(("item","description","kit","assembled"))]
    if not header_words:
        return []
    # Robust relative zones work across scanned US-letter/A4 order forms.
    item_x0,item_x1=0.20*width,0.34*width
    desc_x0,desc_x1=0.34*width,0.78*width
    kit_x0,kit_x1=0.78*width,0.87*width
    asm_x0=0.87*width

    # Group words into visual rows.
    rows=[]
    for w in sorted(words, key=lambda z:(float(z.get("top",0)), float(z.get("x0",0)))):
        y=float(w.get("top",0))
        target=None
        for r in rows[-3:]:
            if abs(r["y"]-y)<=4.0:
                target=r; break
        if target is None:
            target={"y":y,"words":[]}; rows.append(target)
        target["words"].append(w)

    category=""
    brand=""
    if re.search(r"\bIMSAI\b", t, re.I): brand="IMSAI"
    records=[]
    pending=None

    def _row_text(ws,x0,x1=None):
        vals=[]
        for w in sorted(ws,key=lambda z:float(z.get("x0",0))):
            x=float(w.get("x0",0))
            if x>=x0 and (x1 is None or x<x1):
                vals.append(str(w.get("text","")))
        return clean_text(" ".join(vals))

    def _money(ws,x0,x1=None):
        txt=_row_text(ws,x0,x1)
        nums=re.findall(r"\$?\s*(\d{1,6}(?:\.\d{1,2})?)", txt)
        if not nums: return None
        try: return float(nums[-1])
        except Exception: return None

    for r in rows:
        ws=r["words"]
        whole=_row_text(ws,0,None)
        if not whole: continue
        n=norm_header(whole)
        if re.search(r"\btotal prices\b", whole, re.I):
            break
        if re.search(r"\b(?:associates incorporated|republic avenue)\b", whole, re.I):
            continue
        if "item no" in n and "description" in n:
            continue
        # Category rows are short uppercase lines centered around item/description region.
        if re.fullmatch(r"[A-Z0-9 /&\-]{4,45}", whole) and not re.search(r"\$", whole) and            not any(k in n for k in ["item no","description","price","qty","total","terms"]):
            # Only accept obvious section headings.
            if any(k in n for k in ["computer","memory","expansion","boards","modules","peripherals","controllers","miscellaneous","books","shared memory"]):
                category=whole.title()
                continue

        item=_row_text(ws,item_x0,item_x1)
        desc=_row_text(ws,desc_x0,desc_x1)
        kit=_money(ws,kit_x0,kit_x1)
        assembled=_money(ws,asm_x0,None)

        # OCR/PDF extraction may put a currency symbol at the end of description.
        desc=re.sub(r"[.$•·]+\s*$","",desc).strip()
        if item and _looks_like_item_code_v181(item) and desc and len(desc)>=3:
            # Flush previous buffered item.
            if pending:
                records.append(pending)
            price=assembled if assembled is not None else kit
            attrs={"kit_price":kit,"assembled_price":assembled,"selected_price":"assembled" if assembled is not None else "kit"}
            pending={
                "sku":re.sub(r"\s+","-",item.upper()).strip("-"),
                "title":desc,
                "brand":brand,
                "price":price,
                "category":category or "Catalog",
                "size":"",
                "color":"",
                "description":desc,
                "barcode":"",
                "source_sheet":source_name,
                "source_row":int(round(r["y"])),
                "supplier_code":item,
                "import_confidence":"HIGH" if price is not None else "MEDIUM",
                "import_method":"order-form-dual-price",
                "source_page":page_num or "",
                "source_table":"coordinate-order-form",
                "matrix_series":category or "Catalog",
                "matrix_section":"Order form",
                "matrix_model":"",
                "variant_group":item,
                "variant_codes":item,
                "currency":"USD" if "$" in whole or "$" in t else "",
                "vat_note":"",
                "attributes_json":json.dumps(attrs,ensure_ascii=False),
                "quality_confidence":0.96 if price is not None else 0.76,
                "quality_flags":"" if price is not None else "missing_price",
                "category_source":"order-form-heading",
            }
            continue

        # Wrapped descriptions/prices belong to the previous item, never become standalone products.
        if pending:
            if desc and not re.search(r"\b(?:terms|prices|handling|tax|total)\b", desc, re.I):
                if len(pending["description"])<180:
                    pending["description"]=clean_text(pending["description"]+" "+desc)
            if pending["price"] is None and (assembled is not None or kit is not None):
                pending["price"]=assembled if assembled is not None else kit
                try:
                    attrs=json.loads(pending["attributes_json"])
                except Exception:
                    attrs={}
                if kit is not None: attrs["kit_price"]=kit
                if assembled is not None: attrs["assembled_price"]=assembled
                attrs["selected_price"]="assembled" if assembled is not None else "kit"
                pending["attributes_json"]=json.dumps(attrs,ensure_ascii=False)
                pending["import_confidence"]="HIGH"
                pending["quality_confidence"]=0.96
                pending["quality_flags"]=""

    if pending:
        records.append(pending)

    # Require a meaningful table, otherwise fall back to generic parsers.
    good=[r for r in records if r.get("price") is not None and _looks_like_item_code_v181(r.get("supplier_code",""))]
    return records if len(good)>=3 else []


# v1.8.15 regression parsers: standard B2B tables, no-SKU price tables,
# tiered services and vehicle multi-price rows.
def _parse_price_v183(value):
    t=clean_text(value)
    if not t: return None
    t=re.sub(r"(?:USD|EUR|GBP|PLN|UAH|ZAR|INR|AUD|CAD)","",t,flags=re.I)
    t=re.sub(r"[$€£₽]","",t).strip()
    t=re.sub(r"^R(?=\s*\d)","",t,flags=re.I).strip()
    m=re.search(r"[-+]?\d[\d ,.]*\d(?:[.,]\d{1,4})?|[-+]?\d",t)
    if not m: return None
    x=m.group(0).replace(" ","")
    if "," in x and "." in x:
        if x.rfind(",")<x.rfind("."): x=x.replace(",","")
        else: x=x.replace(".","").replace(",",".")
    elif "," in x:
        tail=x.split(",")[-1]
        if len(tail)==3: x=x.replace(",","")
        else: x=x.replace(",",".")
    try: return float(x)
    except Exception: return None

def _header_norm_v183(v):
    return norm_header(clean_text(v).replace("#"," number "))

def extract_standard_commercial_table_v183(raw,source_name,filename="",page_num=None):
    if raw is None or raw.empty or raw.shape[1]<2: return []
    rows=[[clean_text(x) for x in raw.iloc[r].tolist()] for r in range(len(raw))]
    best=None
    for h in range(min(8,len(rows))):
        h1=rows[h]; h2=rows[h+1] if h+1<len(rows) else [""]*len(h1)
        heads=[_header_norm_v183(clean_text(f"{h1[c]} {h2[c] if c<len(h2) else ''}")) for c in range(len(h1))]
        blob=" | ".join(heads); score=0
        if any(k in blob for k in ["price","mrp","national","retail","assembled"]): score+=3
        if any(k in blob for k in ["item number","item no","part no","part number","stock code","vendor part","cat no","support item","product code"]): score+=3
        if any(k in blob for k in ["description","item name","product name","botanical name"]): score+=2
        if best is None or score>best[0]: best=(score,h,heads)
    if not best or best[0]<5:return []
    _,h,heads=best
    def first(terms):
        for c,hd in enumerate(heads):
            if any(t in hd for t in terms): return c
        return None
    sku_col=first(["stock code","vendor part","part no","part number","cat no","support item number","item number","item no","product code"]) 
    if sku_col is None: sku_col=first(["item"])
    title_col=first(["product description","item description","support item name","botanical name","description","product name"])
    brand_col=first(["manufacturer","manuf","oem"]); category_col=first(["category","product category","type"])
    unit_col=first(["uom","unit"]); qty_col=first(["qty","quantity"]); model_col=first(["model"])
    dim_col=first(["dimension"]); weight_col=first(["weight"])
    price_col=None; price_label=""
    for pref in ["proposed price","assembled price","total otr","total retail","incl vat","retail price","list price","unit price","mrp","price","national","base price"]:
        for c,hd in enumerate(heads):
            if pref in hd: price_col=c; price_label=hd; break
        if price_col is not None: break
    if sku_col is None or price_col is None:return []
    if title_col is None:
        cs=[c for c in range(sku_col+1,price_col) if not any(x in heads[c] for x in ["qty","weight","dimension","gst","model","segment"])]
        title_col=cs[0] if cs else None
    if title_col is None:return []
    stops=[c for c in [price_col,brand_col,category_col,unit_col,qty_col,model_col,dim_col,weight_col] if c is not None and c>title_col]
    title_end=min(stops) if stops else price_col
    if title_end<=title_col:title_end=title_col+1
    flat=" ".join(x for row in rows for x in row)
    currency="USD" if "$" in flat else "GBP" if "£" in flat else "EUR" if "€" in flat else "ZAR" if re.search(r"(?:^|\s)R\s*\d",flat,re.I) else ""
    brand_doc=""
    low=" ".join(x.lower() for row in rows[:10] for x in row)
    for b in ["Palo Alto Networks","OPTAVIA","UNICO","Cretors","VARROC","IMSAI"]:
        if b.lower() in low or b.lower() in (filename or "").lower():brand_doc=b
    out=[]; current_category=""; last=None; start=h+1
    if start<len(rows):
        n2=" ".join(_header_norm_v183(x) for x in rows[start])
        if any(k in n2 for k in ["number","remote","available","vat","price"]):start+=1
    for rr in range(start,len(rows)):
        row=rows[rr]; sku=clean_text(row[sku_col]) if sku_col<len(row) else ""
        if not sku or not re.search(r"[A-Za-z0-9]",sku):
            for cc in range(max(0,sku_col-2),min(len(row),sku_col+2)):
                cand=clean_text(row[cc])
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-/]{3,35}",cand) and (re.search(r"[_\-/]",cand) or re.search(r"[A-Za-z]",cand)):
                    sku=cand;break
        price=_parse_price_v183(row[price_col] if price_col<len(row) else "")
        full=clean_text(" ".join(row[c] for c in range(title_col,min(title_end,len(row))) if clean_text(row[c])))
        if sku and price is None and not re.search(r"\d",sku) and len(sku)<=60 and not full:
            current_category=sku;continue
        if not sku and last is not None:
            if full and len(last["description"])<500:last["description"]=clean_text(last["description"]+" "+full)
            continue
        if not sku or price is None:continue
        if sku.isdigit() and len(sku)<4 and "item number" not in heads[sku_col]:continue
        title=full or sku
        if len(title)>120:
            short=re.split(r"[,;]",title,maxsplit=1)[0].strip(); title=short if len(short)>=4 else title[:120].rstrip()
        brand=clean_text(row[brand_col]) if brand_col is not None and brand_col<len(row) else brand_doc
        cat=clean_text(row[category_col]) if category_col is not None and category_col<len(row) else current_category
        attrs={"price_column":price_label}
        for c,hd in enumerate(heads):
            if c!=price_col and any(k in hd for k in ["price","national","remote","mrp"]):
                pv=_parse_price_v183(row[c] if c<len(row) else "")
                if pv is not None:attrs[hd or f"price_col_{c}"]=pv
        if unit_col is not None and unit_col<len(row):attrs["unit"]=clean_text(row[unit_col])
        rec={"sku":sku.upper(),"title":title,"brand":brand,"price":price,"category":cat,"size":"","color":"","description":full or title,
             "barcode":"","source_sheet":source_name,"source_row":rr+1,"supplier_code":sku,"import_confidence":"HIGH","import_method":"standard-commercial-table",
             "source_page":page_num or "","source_table":"native-table","matrix_series":cat,"matrix_section":"Commercial table","matrix_model":"","variant_group":sku,
             "variant_codes":sku,"currency":currency,"vat_note":"","attributes_json":json.dumps(attrs,ensure_ascii=False),"quality_confidence":0.98,"quality_flags":"","category_source":"table" if cat else ""}
        out.append(rec);last=rec
    return out if len(out)>=2 else []

def _synthetic_row_sku_v186(*parts):
    import hashlib
    base="|".join(clean_text(x) for x in parts if clean_text(x))
    return "CAND-"+hashlib.sha1(base.encode("utf-8")).hexdigest()[:12].upper()

def extract_named_price_table_v186(raw,source_name,filename="",page_num=None):
    if raw is None or raw.empty or raw.shape[1]<2:return []
    rows=[[clean_text(x) for x in raw.iloc[i].tolist()] for i in range(len(raw))]
    header_i=None;heads=None;name_col=0;price_cols=[];category=""
    for h in range(min(5,len(rows))):
        h1=rows[h];h2=rows[h+1] if h+1<len(rows) else [""]*len(h1)
        hs=[_header_norm_v183(clean_text(f"{h1[c]} {h2[c] if c<len(h2) else ''}")) for c in range(len(h1))]
        pc=[c for c,hd in enumerate(hs) if any(k in hd for k in ["incl vat","excl vat","total payable","rental rate","retail","price","national","remote"])]
        nc=next((c for c,hd in enumerate(hs) if any(k in hd for k in ["room type","description","item name","product","service","letter","option"])),0)
        if pc:header_i=h;heads=hs;name_col=nc;price_cols=pc;break
    if header_i is None:
        for c in range(1,raw.shape[1]):
            vals=[rows[r][c] for r in range(min(len(rows),12))]
            if sum(_parse_price_v183(v) is not None and bool(re.search(r"[$€£₽]|^R\s*\d",v,re.I)) for v in vals)>=2:price_cols.append(c)
        if not price_cols:return []
        header_i=0;heads=[""]*raw.shape[1]
    chosen=price_cols[0]
    for pref in ["total payable","incl vat","total retail","retail price","national","list price","rental rate","excl vat"]:
        m=next((c for c in price_cols if pref in heads[c]),None)
        if m is not None:chosen=m;break
    flat=" ".join(x for row in rows for x in row)
    currency="GBP" if "£" in flat else "USD" if "$" in flat else "EUR" if "€" in flat else "ZAR" if re.search(r"(?:^|\s)R\s*\d",flat,re.I) else "MYR" if "malaysian ringgit" in flat.lower() else ""
    out=[]
    for rr in range(header_i+1,len(rows)):
        row=rows[rr];name=clean_text(row[name_col]) if name_col<len(row) else ""
        if not name:continue
        p=_parse_price_v183(row[chosen] if chosen<len(row) else "")
        if p is None:
            if len(name)<=60 and not re.search(r"\d{3,}",name):category=name.title()
            continue
        if norm_header(name) in {"optional","total","handling","tax","size","price","list price","retail price"}:continue
        attrs={}
        for c in price_cols:
            pv=_parse_price_v183(row[c] if c<len(row) else "")
            if pv is not None:attrs[heads[c] or f"price_col_{c}"]=pv
        sku=_synthetic_row_sku_v186(filename,page_num,category,name)
        out.append({"sku":sku,"title":name,"brand":"","price":p,"category":category,"size":"","color":"","description":name,"barcode":"",
          "source_sheet":source_name,"source_row":rr+1,"supplier_code":"","import_confidence":"MEDIUM","import_method":"named-price-table","source_page":page_num or "",
          "source_table":"native-table","matrix_series":category,"matrix_section":"Named price table","matrix_model":"","variant_group":name,"variant_codes":"",
          "currency":currency,"vat_note":"","attributes_json":json.dumps(attrs,ensure_ascii=False),"quality_confidence":0.78,
          "quality_flags":"supplier_sku_missing","category_source":"table-heading" if category else ""})
    return out if len(out)>=2 else []

def extract_tiered_service_table_v186(raw,source_name,filename="",page_num=None):
    if raw is None or raw.empty or raw.shape[1]<4:return []
    rows=[[clean_text(x) for x in raw.iloc[i].tolist()] for i in range(len(raw))]
    header=None
    for h in range(min(4,len(rows))):
        hs=[_header_norm_v183(x) for x in rows[h]]
        if any("product code" in x for x in hs) and sum(1 for x in rows[h] if re.search(r"\d.*[-+]",x))>=1:header=(h,hs);break
    if not header:return []
    h,hs=header;code_col=next(i for i,x in enumerate(hs) if "product code" in x)
    tier_cols=[c for c in range(code_col+1,len(hs)) if any(re.fullmatch(r"\d+(?:\.\d+)?p",clean_text(rows[r][c]),re.I) for r in range(h+1,len(rows)) if c<len(rows[r]))]
    if not tier_cols:return []
    title_cols=list(range(0,code_col));prev=[""]*code_col;out=[]
    for rr in range(h+1,len(rows)):
        row=rows[rr];code=clean_text(row[code_col]) if code_col<len(row) else ""
        if not re.fullmatch(r"[A-Z0-9]{2,8}",code,re.I):continue
        parts=[]
        for c in title_cols:
            v=clean_text(row[c]) if c<len(row) else ""
            if v:prev[c]=v
            if prev[c]:parts.append(prev[c])
        title=clean_text(" ".join(parts)) or code;attrs={}
        for c in tier_cols:
            m=re.fullmatch(r"(\d+(?:\.\d+)?)p",clean_text(row[c]),re.I)
            if m:attrs[hs[c] or f"tier_{c}"]=float(m.group(1))/100
        key=hs[tier_cols[0]] or f"tier_{tier_cols[0]}";price=attrs.get(key)
        out.append({"sku":code.upper(),"title":title,"brand":"Royal Mail" if "royal" in (filename or "").lower() else "","price":price,"category":"Mail service",
          "size":"","color":"","description":title,"barcode":"","source_sheet":source_name,"source_row":rr+1,"supplier_code":code,"import_confidence":"HIGH",
          "import_method":"standard-commercial-table","source_page":page_num or "","source_table":"tiered-service","matrix_series":"Mail service","matrix_section":"Tiered price",
          "matrix_model":"","variant_group":code,"variant_codes":code,"currency":"GBP","vat_note":"","attributes_json":json.dumps(attrs,ensure_ascii=False),
          "quality_confidence":0.98,"quality_flags":"","category_source":"service-table"})
    return out if len(out)>=2 else []

def extract_vehicle_price_v186(page_text,source_name,filename="",page_num=None):
    raw=str(page_text or "");low=clean_text(raw).lower()
    if not ("total" in low and "price" in low and ("otr" in low or "retail" in low) and "£" in raw):return []
    recs=[]
    for line_no,line in enumerate(raw.splitlines(),1):
        line=clean_text(line);monies=list(re.finditer(r"£\s*([0-9][0-9,]*(?:\.\d{1,2})?)",line))
        if len(monies)<3:continue
        vals=[_parse_price_v183(m.group(0)) for m in monies]
        prefix=clean_text(line[:monies[0].start()])
        mcode=re.match(r"(.+?)\s+([A-Z0-9][A-Z0-9.\-]{2,20})\s+\d+(?:\s+\d+){0,3}$",prefix,re.I)
        if mcode:title,code=clean_text(mcode.group(1)),mcode.group(2)
        else:
            mo=re.match(r"(.+?)\s+([A-Z][A-Z0-9]{1,6})$",prefix)
            if not mo:continue
            title,code=clean_text(mo.group(1)),mo.group(2)
        if code.lower() in {"total","vat","basic","price","otr","retail","charges"} or len(title)<3:continue
        attrs={"basic_price":vals[0],"vat":vals[1],"total_retail":vals[2]}
        if len(vals)>3:attrs["otr_charges"]=vals[3]
        if len(vals)>4:attrs["total_otr"]=vals[4]
        price=attrs.get("total_otr") or attrs.get("total_retail") or vals[0]
        category="Vehicle option" if "options" in low and len(vals)==3 else "Vehicle"
        recs.append({"sku":code.upper(),"title":title,"brand":"Fiat" if "fiat" in low or "fiat" in (filename or "").lower() else "","price":price,"category":category,
          "size":"","color":"","description":title,"barcode":"","source_sheet":source_name,"source_row":line_no,"supplier_code":code,"import_confidence":"HIGH",
          "import_method":"vehicle-price-row","source_page":page_num or "","source_table":"vehicle-price","matrix_series":category,"matrix_section":"Model price","matrix_model":code,
          "variant_group":title,"variant_codes":code,"currency":"GBP","vat_note":"VAT included in total retail/OTR","attributes_json":json.dumps(attrs,ensure_ascii=False),
          "quality_confidence":0.98,"quality_flags":"","category_source":"vehicle-price"})
    seen=set();out=[]
    for r in recs:
        if r["sku"] not in seen:seen.add(r["sku"]);out.append(r)
    return out


def _parse_pdf_page_v13(page, page_num, page_text, filename=""):
    page_name=f"PDF p.{page_num}"
    page_series_hint=_series_from_page_text(page_text) or _generic_page_series(page_text,page_num)
    header_count=pattern_count=matrix_count=dimension_count=row_count=0
    source_rows=source_columns=0; records=[]

    vehicle_records=extract_vehicle_price_v186(page_text,page_name,filename=filename,page_num=page_num)
    if vehicle_records:
        return vehicle_records,{"sheet":page_name,"source_rows":len(vehicle_records),"source_columns":0,"matrix_products":0,"dimension_products":0,"row_price_products":len(vehicle_records),"header_products":0,"pattern_products":0,"product_card_products":0,"scan_status":"parsed-vehicle-price"}

    order_records=extract_order_form_products_v181(page,page_text,page_name,filename=filename,page_num=page_num)
    if order_records:
        return order_records,{"sheet":page_name,"source_rows":len(order_records),"source_columns":0,"matrix_products":0,"dimension_products":0,"row_price_products":len(order_records),"header_products":0,"pattern_products":0,"product_card_products":0,"scan_status":"parsed-order-form"}

    card_records=extract_product_card_products_from_text(page_text,page_name,filename=filename,page_num=page_num)
    if card_records:
        return card_records,{"sheet":page_name,"source_rows":len([x for x in page_text.splitlines() if clean_text(x)]),"source_columns":0,"matrix_products":0,"dimension_products":0,"row_price_products":0,"header_products":0,"pattern_products":0,"product_card_products":len(card_records),"scan_status":"parsed-product-card"}

    table_raws=_pdf_tables_to_raw(page)
    for table_idx,raw in enumerate(table_raws,start=1):
        if raw.empty:continue
        source_rows+=len(raw);source_columns=max(source_columns,raw.shape[1])

        tiered=extract_tiered_service_table_v186(raw,f"{page_name} T{table_idx}",filename=filename,page_num=page_num)
        if tiered:row_count+=len(tiered);records.extend(tiered);continue

        standard=extract_standard_commercial_table_v183(raw,f"{page_name} T{table_idx}",filename=filename,page_num=page_num)
        if standard:row_count+=len(standard);records.extend(standard);continue

        named=extract_named_price_table_v186(raw,f"{page_name} T{table_idx}",filename=filename,page_num=page_num)
        if named:row_count+=len(named);records.extend(named);continue

        matrix_records=extract_matrix_products(raw,f"{page_name} T{table_idx}",filename=filename,page_num=page_num,table_index=table_idx,series_hint=page_series_hint)
        if matrix_records:matrix_count+=len(matrix_records);records.extend(matrix_records);continue

        dimension_records=extract_dimension_matrix_products(raw,f"{page_name} T{table_idx}",filename=filename,page_num=page_num,table_index=table_idx,series_hint=page_series_hint)
        if dimension_records:dimension_count+=len(dimension_records);records.extend(dimension_records);continue

        row_records=extract_tabular_price_products(raw,f"{page_name} T{table_idx}",filename=filename,page_num=page_num,table_index=table_idx,series_hint=page_series_hint)
        if row_records:row_count+=len(row_records);records.extend(row_records);continue

        brand=_infer_brand(raw,filename)
        headers=extract_header_blocks(raw,f"{page_name} T{table_idx}",brand=brand)
        patterns=extract_pattern_products(raw,f"{page_name} T{table_idx}",filename=filename)
        header_count+=len(headers);pattern_count+=len(patterns);records.extend(headers);records.extend(patterns)

    if matrix_count==0 and dimension_count==0:
        line_raw=_pdf_words_to_raw(page)
        if not line_raw.empty:
            source_rows+=len(line_raw);source_columns=max(source_columns,line_raw.shape[1])
            mr=extract_matrix_products(line_raw,page_name,filename=filename,page_num=page_num,table_index="words",series_hint=page_series_hint)
            if mr:matrix_count+=len(mr);records.extend(mr)
            else:
                dr=extract_dimension_matrix_products(line_raw,page_name,filename=filename,page_num=page_num,table_index="words",series_hint=page_series_hint)
                if dr:dimension_count+=len(dr);records.extend(dr)

    if matrix_count==0 and dimension_count==0 and row_count==0 and not card_records and _looks_like_price_page(page_text):
        rr=extract_row_price_products_from_text(page_text,page_name,filename=filename,page_num=page_num,series_hint=page_series_hint)
        if rr:row_count+=len(rr);records.extend(rr)

    report_row={"sheet":page_name,"source_rows":source_rows,"source_columns":source_columns,
      "matrix_products":matrix_count,"dimension_products":dimension_count,"row_price_products":row_count,
      "header_products":header_count,"pattern_products":pattern_count,"product_card_products":len(card_records),"scan_status":"parsed"}
    return records,report_row

# -----------------------------
# v1.7 Hybrid Quality Intelligence
# -----------------------------
def _attrs_dict(value):
    try:
        return json.loads(value or "{}") if not isinstance(value, dict) else dict(value)
    except Exception:
        return {}

def _bbox_from_record(row):
    a=_attrs_dict(row.get("attributes_json", ""))
    bb=a.get("sku_bbox") or a.get("anchor_bbox")
    try:
        return tuple(float(x) for x in bb) if bb and len(bb)==4 else None
    except Exception:
        return None

def _bbox_iou(a,b):
    if not a or not b: return 0.0
    x1=max(a[0],b[0]); y1=max(a[1],b[1]); x2=min(a[2],b[2]); y2=min(a[3],b[3])
    inter=max(0,x2-x1)*max(0,y2-y1)
    aa=max(0,a[2]-a[0])*max(0,a[3]-a[1]); ab=max(0,b[2]-b[0])*max(0,b[3]-b[1])
    return inter/max(aa+ab-inter,1.0)

def _dimension_sanity_v17(value):
    """Conservative OCR dimension repair. Never changes ambiguous values silently."""
    raw=clean_text(value).replace("×","x").replace("*","x")
    if not raw: return raw, "", 1.0
    m=re.search(r"(\d{2,5})\s*x\s*(\d{1,4})(?:\s*x\s*(\d{1,4}))?\s*(mm|cm)?", raw, re.I)
    if not m: return raw, "", 1.0
    vals=[int(v) for v in m.groups()[:3] if v]
    unit=(m.group(4) or "mm").lower()
    original=list(vals); changed=False
    # Typical hand-held catalog products: a first dimension > 1000 next to two sub-500
    # dimensions is usually an OCR digit insertion. Suggest, but only auto-fix when one
    # single digit deletion yields a plausible 20..999 mm value.
    if unit=="mm" and len(vals)>=2 and vals[0]>1000 and all(v<500 for v in vals[1:]):
        text=str(vals[0]); candidates=[]
        for i in range(len(text)):
            q=text[:i]+text[i+1:]
            if q and 20 <= int(q) <= 999: candidates.append(int(q))
        if len(set(candidates))==1:
            vals[0]=candidates[0]; changed=True
    normalized="x".join(map(str,vals))+unit
    suggestion=normalized if changed else ""
    return normalized if changed else raw, suggestion, 0.92 if changed else 1.0

def _clean_title_v17(title, sku, category, variant_group="", import_method="", description=""):
    """Conservative title cleanup.

    v1.7.6 rule: coherent product-card titles are authoritative and must never be
    replaced by the SKU merely because they wrap across many words. Variant families
    inherit the common family title from ``variant_group``. Generic visual/row parsers
    keep the older defensive cleanup.
    """
    t=clean_text(title); vg=clean_text(variant_group); method=clean_text(import_method)
    desc=clean_text(description)

    if method in {"standard-commercial-table","commercial-text-row","order-form-dual-price","vehicle-price-row"}:
        return t or desc or vg or sku

    # Product-card variants: the family title is stronger evidence than a numeric title.
    if method == "product-card-variant":
        if vg and vg != sku and not _looks_like_marketing_copy(vg):
            return vg
        if (not t or t == sku or re.fullmatch(r"\d{5}", t)) and desc and desc != sku and not _looks_like_marketing_copy(desc):
            return desc
        return t or vg or sku

    # Explicit product cards may legitimately have long names. Preserve them unless
    # the title itself is clearly promotional/noise. Recover from description if needed.
    if method == "product-card":
        if (not t or t == sku or re.fullmatch(r"\d{5}", t)) and desc and desc != sku and not _looks_like_marketing_copy(desc):
            t=desc
        if t and not _looks_like_marketing_copy(t):
            return t
        if desc and desc != sku and not _looks_like_marketing_copy(desc):
            return desc
        return t or sku

    # Older defensive behaviour for generic row/visual parsers.
    if _looks_like_marketing_copy(t) or len(t.split())>9:
        if category and category!="Visual Catalog": t=f"{category} {sku}".strip()
        elif vg and not _looks_like_marketing_copy(vg) and len(vg.split())<=8: t=vg
        else: t=sku
    if sku and category and category!="Visual Catalog" and sku in t and len(t.split())>6:
        t=f"{category} {sku}"
    return t

def _plausible_recovered_title(value, sku=""):
    t=clean_text(value)
    if not t or t == clean_text(sku) or len(t) < 4:
        return False
    if re.fullmatch(r"\d{5}", t):
        return False
    low=t.lower()
    if any(x in low for x in _PROMO_PHRASES):
        return False
    if re.search(r"\b(?:подробнее|скидка|закажи|страниц[аеуы]?|часов|недель)\b", low):
        return False
    return bool(re.search(r"[A-Za-zА-Яа-яЁё]", t))


# v1.7.7 Text Sanity Gate + Title Repair
_CID_RE = re.compile(r"\(cid:\d+\)", re.I)
_SYMBOL_CLUSTER_RE = re.compile(r"(?:!#\!|!\$!|\$\"|[#@$%^&*_+=~]{2,})")
_FOREIGN_SKU_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")
_BROKEN_END_RE = re.compile(r"\b(?:для|и|или|с|со|в|во|на|по|из|от|до|при|к|ко|у|без|под|над|за)\s*$", re.I)

def _sanitize_catalog_text_v177(value):
    """Remove PDF glyph artefacts without rewriting normal product wording."""
    t=clean_text(value)
    if not t:
        return ""
    t=_CID_RE.sub(" ", t)
    t=_SYMBOL_CLUSTER_RE.sub(" ", t)
    # Isolated exclamation/quote markers are common PDF extraction artefacts around cards.
    t=re.sub(r"(?:(?<=\s)|^)[!#$%]+(?=\s|$)", " ", t)
    t=re.sub(r"\s+([,.;:!?])", r"\1", t)
    t=re.sub(r"([,.;:!?]){3,}", r"\1", t)
    t=re.sub(r"\s{2,}", " ", t).strip(" \t|;,:-–—")
    return clean_text(t)

def _title_sanity_flags_v177(title, sku=""):
    raw=clean_text(title); t=_sanitize_catalog_text_v177(raw); flags=[]
    if not t:
        return ["title_empty_after_repair"]
    low=t.lower()
    if _CID_RE.search(raw): flags.append("pdf_cid_glyphs")
    if _SYMBOL_CLUSTER_RE.search(raw): flags.append("pdf_symbol_noise")
    if any(x in low for x in _PROMO_PHRASES) or re.search(r"\b(?:подробнее|скидка|закажи|предложение действительно)\b", low):
        flags.append("promo_or_noise_title")
    letters=len(re.findall(r"[A-Za-zА-Яа-яЁё]", t)); odd=len(re.findall(r"[^A-Za-zА-Яа-яЁё0-9 .,'’()\-/+]", t))
    if letters and odd/max(len(t),1) > 0.08:
        flags.append("title_symbol_ratio")
    if _BROKEN_END_RE.search(t):
        flags.append("title_fragment")
    codes=[m.group(1) for m in _FOREIGN_SKU_RE.finditer(t)]
    foreign=[c for c in codes if c != clean_text(sku)]
    if foreign:
        flags.append("foreign_sku_in_title")
    # Obvious prose spill: sentence-like copy is not a catalogue title.
    if len(t.split()) > 18 or (len(t.split()) > 12 and re.search(r"[.!?]", t)):
        flags.append("title_too_long")
    if re.search(r"\b(?:материал|размер|состав)\s*:", t, re.I) or re.search(r"\bне является лекарством\b", t, re.I):
        flags.append("body_copy_spill")
    # A pack size embedded in the middle of a title is a common two-column PDF merge artefact.
    if re.search(r"\b\d+(?:[,.]\d+)?\s*(?:мл|ml|г|g)\.?\s+\S+", t, re.I):
        flags.append("size_inside_title")
    if re.search(r"\bконцентраци[яи]\s+аромата\b", t, re.I):
        flags.append("fragrance_copy_spill")
    # Detect immediate repeated 2-5 word phrases caused by duplicated PDF text layers.
    words=t.lower().split()
    for n in range(5,1,-1):
        if any(words[j:j+n] == words[j+n:j+2*n] for j in range(0, max(0,len(words)-2*n+1))):
            flags.append("repeated_title_phrase"); break
    return list(dict.fromkeys(flags))

def _repair_title_v177(title, sku="", description="", variant_group="", method=""):
    """Conservatively repair title text; never invent wording absent from parsed fields."""
    t=_sanitize_catalog_text_v177(title)
    desc=_sanitize_catalog_text_v177(description)
    vg=_sanitize_catalog_text_v177(variant_group)
    sku=clean_text(sku); method=clean_text(method)

    if method in {"standard-commercial-table","commercial-text-row","order-form-dual-price","vehicle-price-row"}:
        return t or desc or vg or sku

    # Variant family title is authoritative when sane.
    if method == "product-card-variant" and vg and not _title_sanity_flags_v177(vg, sku):
        t=vg

    # If extraction left a code or broken fragment, prefer a sane description already present.
    bad=set(_title_sanity_flags_v177(t, sku))
    if (not t or t == sku or bad.intersection({"title_fragment","title_too_long","promo_or_noise_title"})) and desc:
        dflags=set(_title_sanity_flags_v177(desc, sku))
        if not dflags.intersection({"promo_or_noise_title","title_too_long","foreign_sku_in_title","title_fragment"}) and desc != sku:
            t=desc

    # Remove own SKU if the parser prefixed/suffixed it to an otherwise readable title.
    if sku and re.fullmatch(r"\d{5}", sku):
        t=re.sub(rf"^\s*{re.escape(sku)}\s*[-:–—]?\s*", "", t).strip()
        t=re.sub(rf"\s*[-:–—]?\s*{re.escape(sku)}\s*$", "", t).strip()

    return _sanitize_catalog_text_v177(t)

def text_sanity_repair_v177(imported):
    if imported is None or imported.empty:
        return imported, {"text_titles_repaired":0,"text_rows_flagged":0}
    df=imported.copy().reset_index(drop=True)
    repaired=0; flagged=0
    if "quality_flags" not in df.columns:
        df["quality_flags"]=""
    else:
        df["quality_flags"]=df["quality_flags"].astype("object")
    for i,r in df.iterrows():
        sku=clean_text(r.get("sku","")); old=clean_text(r.get("title","")); method=clean_text(r.get("import_method",""))
        new=_repair_title_v177(old, sku, r.get("description",""), r.get("variant_group",""), method)
        if new != old:
            df.at[i,"title"]=new; repaired += 1
        # Clean obvious PDF artefacts from supporting text too, without changing semantics.
        for col in ("description","variant_group","color"):
            if col in df.columns:
                cur=clean_text(r.get(col,"")); clean=_sanitize_catalog_text_v177(cur)
                if clean != cur:
                    df.at[i,col]=clean
        flags=[x.strip() for x in clean_text(df.at[i,"quality_flags"]).split(",") if x.strip()]
        if method in {"standard-commercial-table","commercial-text-row","order-form-dual-price","vehicle-price-row"}:
            sanity=[f for f in _title_sanity_flags_v177(df.at[i,"title"], sku)
                    if f in {"pdf_cid_glyphs","pdf_symbol_noise","title_empty_after_repair","repeated_title_phrase"}]
        else:
            sanity=_title_sanity_flags_v177(df.at[i,"title"], sku)
        if sanity:
            flagged += 1
            for f in sanity:
                if f not in flags: flags.append(f)
        df.at[i,"quality_flags"] = ", ".join(flags)
    return df, {"text_titles_repaired":repaired,"text_rows_flagged":flagged}



# v1.7.8 Final Title Polish
# This pass is intentionally narrow: it only removes orphan PDF punctuation/glyph
# tokens after a title has already been recovered. It never invents or reorders words.
_ORPHAN_GLYPH_TOKEN_RE = re.compile(
    r"(?x)(?:^|\s)[\"'“”„«»‘’`´]*[!#$%^*_~=]{1,4}[\"'“”„«»‘’`´]*(?=\s|$)"
)
_TRAILING_GLYPH_RE = re.compile(r"(?:\s*[\"'“”„«»‘’`´]*[!#$%^*_~=]{1,4}[\"'“”„«»‘’`´]*)+$")
_LEADING_GLYPH_RE = re.compile(r"^(?:[\"'“”„«»‘’`´]*[!#$%^*_~=]{1,4}[\"'“”„«»‘’`´]*\s*)+")


def _final_title_polish_v178(value):
    """Remove only obvious orphan punctuation introduced by PDF text extraction.

    Examples fixed safely: ``Шампунь ... \"!\"`` and standalone ``\"#\"``/``\"%\"``
    tokens. Normal punctuation inside names (hyphens, apostrophes, parentheses,
    ampersands, slashes) is preserved.
    """
    t=_sanitize_catalog_text_v177(value)
    if not t:
        return ""
    prev=None
    while prev != t:
        prev=t
        t=_ORPHAN_GLYPH_TOKEN_RE.sub(" ", t)
        t=_TRAILING_GLYPH_RE.sub("", t)
        t=_LEADING_GLYPH_RE.sub("", t)
        t=re.sub(r'["“”„«»]\s*["“”„«»]', " ", t)
        t=re.sub(r"\s{2,}", " ", t).strip(" \t|;,:-–—")
    return clean_text(t)


def final_title_polish_v178(imported):
    """Final conservative cleanup + residual-glyph QA flagging."""
    if imported is None or imported.empty:
        return imported, {"final_titles_polished":0,"residual_title_glyphs":0}
    df=imported.copy().reset_index(drop=True)
    polished=0; residual=0
    if "quality_flags" not in df.columns:
        df["quality_flags"]=""
    else:
        df["quality_flags"]=df["quality_flags"].astype("object")
    for i,r in df.iterrows():
        old=clean_text(r.get("title",""))
        new=_final_title_polish_v178(old)
        if new != old:
            df.at[i,"title"]=new; polished += 1
        flags=[x.strip() for x in clean_text(df.at[i,"quality_flags"]).split(",") if x.strip()]
        if re.search(r'(?:^|\s)["\'“”„«»‘’`´]*[!#$%^*_~=]{1,4}["\'“”„«»‘’`´]*(?:\s|$)', new):
            residual += 1
            if "residual_pdf_punct" not in flags:
                flags.append("residual_pdf_punct")
        df.at[i,"quality_flags"] = ", ".join(flags)
    return df, {"final_titles_polished":polished,"residual_title_glyphs":residual}


def _split_embedded_sku_title(title, primary_sku=""):
    """Split a title accidentally containing another 5-digit product code.

    Returns ``(left_title, [(sku, right_title), ...])``.  The split is intentionally
    conservative: a secondary code must have a human-readable tail and promo phrases
    are rejected. This fixes side-by-side catalogue cards without turning arbitrary
    legal/promo references into products.
    """
    t=clean_text(title)
    if not t:
        return t, []
    hits=list(_NUMERIC_SKU_RE.finditer(t))
    extras=[]
    keep=t
    # Ignore a leading occurrence matching the record's own SKU.
    for idx,m in enumerate(hits):
        code=m.group(1)
        if code == clean_text(primary_sku) and m.start() <= 2:
            continue
        end=hits[idx+1].start() if idx+1 < len(hits) else len(t)
        tail=clean_text(t[m.end():end]).strip(" .,:;-–—")
        if not _plausible_recovered_title(tail, code):
            continue
        # Avoid shade/variant-style tails: one or two color words normally belong to
        # a family card and are handled by product-card-variant, not multi-card split.
        if len(tail.split()) <= 2 and _is_variant_label(tail):
            continue
        extras.append((code, tail))
        if m.start() > 0:
            keep=clean_text(t[:m.start()]).strip(" .,:;-–—")
        break
    return keep or t, extras


def title_recovery_and_multicard_v176(imported):
    """Recover titles lost to QA cleanup and split obvious side-by-side product cards.

    Runs after SKU dedupe and before the quality layer. No SKU is invented: every new
    record must contain a literal 5-digit code already present in the parsed title/
    description. Ambiguous splits are retained for review rather than forced READY.
    """
    if imported is None or imported.empty:
        return imported, {"titles_recovered":0, "multicards_split":0}
    df=imported.copy().reset_index(drop=True)
    recovered=0; new_rows=[]

    for i,r in df.iterrows():
        sku=clean_text(r.get("sku","")); method=clean_text(r.get("import_method",""))
        title=clean_text(r.get("title","")); desc=clean_text(r.get("description","")); vg=clean_text(r.get("variant_group",""))

        # Strongest title recovery path for card/variant parsers.
        if method == "product-card-variant" and _plausible_recovered_title(vg, sku):
            if title != vg:
                df.at[i,"title"] = vg; title=vg; recovered += 1
        elif method == "product-card" and (not _plausible_recovered_title(title, sku)) and _plausible_recovered_title(desc, sku):
            df.at[i,"title"] = desc; title=desc; recovered += 1

        # Side-by-side cards can be flattened by the PDF text layer into one title or
        # description. Only split explicit product cards; family variants are handled
        # separately by _shade_pairs.
        if method != "product-card":
            continue
        source=title
        if not any(m.group(1) != sku for m in _NUMERIC_SKU_RE.finditer(source)) and desc != source:
            source=desc
        left, extras=_split_embedded_sku_title(source, sku)
        if extras and _plausible_recovered_title(left, sku):
            df.at[i,"title"] = left
            if desc == source:
                df.at[i,"description"] = left
            for code, extra_title in extras:
                nr=r.copy()
                nr["sku"]=code; nr["supplier_code"]=code; nr["title"]=extra_title; nr["description"]=extra_title
                nr["variant_group"]=""; nr["variant_codes"]=""
                nr["import_method"]="product-card-split"
                nr["quality_confidence"]=0.78
                old_flags=clean_text(nr.get("quality_flags",""))
                nr["quality_flags"] = ",".join(x for x in [old_flags,"multicard_split_review"] if x)
                new_rows.append(nr.to_dict())

    if new_rows:
        df=pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True, sort=False)
    return df, {"titles_recovered":recovered, "multicards_split":len(new_rows)}


# v1.7.9 Catalog Context + Conservative Taxonomy
# These rules infer only broad product classes from literal title/description words.
# They never invent SKU, price, shade, size, or product-specific claims.
_CATEGORY_RULES_V179 = (
    ("Макияж / Тушь для ресниц", (r"\bтуш(?:ь|и)\b", r"\bmascara\b", r"wonderlash")),
    ("Макияж / Губы", (r"помад", r"блеск.{0,12}губ", r"карандаш.{0,12}губ", r"lip(?:stick|gloss|liner)")),
    ("Макияж / Лицо", (r"тональн", r"консилер", r"пудр", r"румян", r"хайлайтер", r"бронзер", r"foundation", r"concealer")),
    ("Дезодоранты", (r"дезодоран", r"антиперспиран", r"deodorant", r"antiperspirant")),
    ("Парфюмерия", (r"туалетн.{0,10}вод", r"парфюмерн.{0,10}вод", r"\bдухи\b", r"eau de", r"fragrance", r"парфюмированн.{0,16}спрей")),
    ("Уход за телом", (r"крем.{0,16}тела", r"лосьон.{0,16}тела", r"гель.{0,16}душ", r"скраб.{0,16}тела", r"body (?:cream|lotion|wash|scrub)")),
    ("Уход за волосами", (r"шампун", r"кондиционер.{0,16}волос", r"маск.{0,16}волос", r"сыворотк.{0,16}волос", r"пре-шампун", r"расч[её]ск", r"щ[её]тк.{0,16}волос", r"hair (?:shampoo|conditioner|serum|mask|brush)")),
    ("Уход за лицом", (r"крем.{0,16}лиц", r"сыворотк.{0,16}лиц", r"умыван", r"очищен.{0,12}лиц", r"тоник.{0,12}лиц", r"face (?:cream|serum|cleanser|toner)")),
    ("Wellness / Пищевые добавки", (r"биологически активн", r"мультивитамин", r"омега-?3", r"витамин\s+[a-zа-я0-9]", r"питательн.{0,16}коктейл", r"пребиот", r"кальци", r"wellness", r"orimetabo")),
    ("Аксессуары", (r"кошел[её]к", r"косметичк", r"\bсумк", r"инструмент", r"аксессуар", r"\bbrush\b", r"\bcomb\b")),
)

def _infer_category_v179(title, description=""):
    text=clean_text(f"{title} {description}").lower()
    if not text:
        return ""
    for category, patterns in _CATEGORY_RULES_V179:
        if any(re.search(p, text, re.I) for p in patterns):
            return category
    return ""

def _dominant_catalog_brand_v179(df):
    vals=[clean_text(v) for v in df.get("brand", pd.Series(dtype=object)).tolist() if clean_text(v)]
    if len(vals) < 5:
        return ""
    counts=Counter(vals)
    brand,n=counts.most_common(1)[0]
    # Require a strong single-brand signal before propagating document context.
    return brand if n / max(len(vals),1) >= 0.85 else ""

def _repair_body_copy_tail_v179(title):
    t=clean_text(title)
    if not t:
        return ""
    # Safe hard boundaries: everything after these labels is specification/legal copy, not a title.
    parts=re.split(r"\b(?:Не является лекарством|Материал\s*:|Способ применения\s*:|Применение\s*:|Принимай\b|Нанеси\b)", t, maxsplit=1, flags=re.I)
    candidate=clean_text(parts[0]).strip(" .,:;-–—")
    return candidate if len(candidate) >= 4 else t

def quality_intelligence_v17(imported):
    """Cross-page quality layer: category memory, conservative size sanity, title cleanup,
    spatial/size duplicate suppression and per-row quality confidence.
    """
    if imported is None or imported.empty:
        return imported, {"input_rows":0,"output_rows":0,"duplicates_removed":0,"categories_inherited":0,"dimensions_corrected":0,"titles_cleaned":0}
    df=imported.copy().reset_index(drop=True)

    # v1.7.1 dtype safety: pandas 3.x no longer allows assigning floats into
    # StringDtype columns. Keep confidence values numeric and QA text fields object.
    if "quality_confidence" not in df.columns:
        df["quality_confidence"] = pd.Series([float("nan")] * len(df), dtype="float64")
    else:
        df["quality_confidence"] = pd.to_numeric(df["quality_confidence"], errors="coerce").astype("float64")

    for c in ["quality_flags","category_source","dimension_original","dimension_suggestion"]:
        if c not in df.columns:
            df[c] = pd.Series([""] * len(df), dtype="object")
        else:
            df[c] = df[c].astype("object")

    # These fields may arrive from OCR/checkpoints with mixed types. Normalize them
    # before quality calculations so newer pandas versions cannot fail late in a scan.
    for c in ["visual_confidence", "ocr_confidence", "scanner_confidence"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")

    stats={"input_rows":len(df),"duplicates_removed":0,"categories_inherited":0,"dimensions_corrected":0,"titles_cleaned":0,
           "categories_inferred":0,"brands_inferred":0,"body_tails_trimmed":0}

    # v1.7.9 whole-catalog context. Fill brand only when the document already provides
    # a strong single-brand signal; infer only broad categories from literal product words.
    dominant_brand=_dominant_catalog_brand_v179(df)
    for i,r in df.iterrows():
        method=clean_text(r.get("import_method",""))
        if dominant_brand and not clean_text(r.get("brand","")) and method.startswith("product-card"):
            df.at[i,"brand"]=dominant_brand
            stats["brands_inferred"]+=1
            flags=[x.strip() for x in clean_text(r.get("quality_flags","")).split(",") if x.strip()]
            if "brand_inferred_catalog" not in flags: flags.append("brand_inferred_catalog")
            df.at[i,"quality_flags"] = ", ".join(flags)
        cat=clean_text(r.get("category",""))
        if not cat or cat=="Visual Catalog":
            inferred=_infer_category_v179(r.get("title",""), r.get("description",""))
            if inferred:
                df.at[i,"category"]=inferred
                df.at[i,"category_source"]="title-rule"
                stats["categories_inferred"]+=1
        old_title=clean_text(r.get("title",""))
        trimmed=_repair_body_copy_tail_v179(old_title)
        if trimmed != old_title:
            df.at[i,"title"]=trimmed
            if clean_text(r.get("description","")) == old_title:
                df.at[i,"description"]=trimmed
            stats["body_tails_trimmed"]+=1

    # Page context memory. Only propagate specific categories through short runs of visual pages.
    page_cat={}
    for _,r in df.iterrows():
        try: p=int(float(r.get("source_page") or 0))
        except: p=0
        cat=clean_text(r.get("category",""))
        if p and cat and cat!="Visual Catalog": page_cat.setdefault(p,[]).append(cat)
    dominant={p:Counter(v).most_common(1)[0][0] for p,v in page_cat.items() if v}
    last_cat=""; last_page=-99
    for i,r in df.iterrows():
        try: p=int(float(r.get("source_page") or 0))
        except: p=0
        cat=clean_text(r.get("category",""))
        if p in dominant:
            last_cat=dominant[p]; last_page=p
            if not cat or cat=="Visual Catalog":
                df.at[i,"category"]=last_cat; df.at[i,"category_source"]="same-page-context"; stats["categories_inherited"]+=1
        elif (not cat or cat=="Visual Catalog") and last_cat and p and 0 < p-last_page <= 3:
            df.at[i,"category"]=last_cat; df.at[i,"category_source"]="previous-page-context"; stats["categories_inherited"]+=1
        elif cat and cat!="Visual Catalog": df.at[i,"category_source"]="direct"
    # Dimension sanity + strict title cleanup.
    for i,r in df.iterrows():
        old_size=clean_text(r.get("size","")); new_size,sugg,sconf=_dimension_sanity_v17(old_size)
        if sugg:
            df.at[i,"dimension_original"]=old_size; df.at[i,"dimension_suggestion"]=sugg; df.at[i,"size"]=new_size; stats["dimensions_corrected"]+=1
        old_title=clean_text(r.get("title","")); new_title=_clean_title_v17(
            old_title, clean_text(r.get("sku","")), clean_text(df.at[i,"category"]),
            clean_text(r.get("variant_group","")), clean_text(r.get("import_method","")),
            clean_text(r.get("description",""))
        )
        if new_title!=old_title: df.at[i,"title"]=new_title; stats["titles_cleaned"]+=1
    # Quality duplicate killer. Exact supplier SKU already merged upstream; this targets
    # generated visual cards that overlap or repeat on the same page.
    drop=set(); groups=[]
    visual=df[df["import_method"].astype(str).str.contains("visual",case=False,na=False)]
    by_page={}
    for i,r in visual.iterrows():
        try: p=int(float(r.get("source_page") or 0))
        except: p=0
        by_page.setdefault(p,[]).append(i)
    gid=0
    for p,idxs in by_page.items():
        for a_pos,a in enumerate(idxs):
            if a in drop: continue
            ra=df.loc[a]; bba=_bbox_from_record(ra); sa=clean_text(ra.get("size","")); skua=clean_text(ra.get("supplier_code",""))
            for b in idxs[a_pos+1:]:
                if b in drop: continue
                rb=df.loc[b]; bbb=_bbox_from_record(rb); sb=clean_text(rb.get("size","")); skub=clean_text(rb.get("supplier_code",""))
                same_generated=(not skua and not skub)
                overlap=_bbox_iou(bba,bbb)
                same_size=bool(sa and sb and sa==sb)
                # Require strong spatial overlap, or near-identical generated card anchors.
                duplicate = same_generated and (overlap>=0.55 or (same_size and overlap>=0.25))
                if duplicate:
                    ca=float(ra.get("visual_confidence") or 0); cb=float(rb.get("visual_confidence") or 0)
                    winner,loser=(a,b) if ca>=cb else (b,a)
                    drop.add(loser); gid+=1
                    groups.append((gid,winner,loser,p,round(max(overlap,0),3)))
    if drop:
        stats["duplicates_removed"]=len(drop); df=df.drop(index=list(drop)).reset_index(drop=True)
    # Confidence and flags after transformations.
    for i,r in df.iterrows():
        flags=[x.strip() for x in clean_text(r.get("quality_flags","")).split(",") if x.strip()]
        try:
            v=float(r.get("visual_confidence") or 0)
            if pd.isna(v): v=0.0
        except Exception:
            v=0.0
        base=v if v else (0.94 if clean_text(r.get("import_confidence"))=="HIGH" else 0.78 if clean_text(r.get("import_confidence"))=="MEDIUM" else 0.58)
        cat=clean_text(r.get("category","")); title=clean_text(r.get("title","")); supplier=clean_text(r.get("supplier_code",""))
        method=clean_text(r.get("import_method","")); sku=clean_text(r.get("sku","")); color=clean_text(r.get("color",""))
        if not supplier and (sku.startswith("VIS-") or sku.startswith("CAND-") or method in {"visual-card-fallback","visual-named-price-card"}): flags.append("supplier_sku_missing"); base-=.12
        if not cat or cat=="Visual Catalog": flags.append("generic_category"); base-=.05
        if not title or title==sku: flags.append("weak_title"); base-=.12
        low_title=title.lower()
        if any(x in low_title for x in _PROMO_PHRASES) or re.search(r"\b(?:подробнее|скидка|закажи|часов|недель)\b", low_title):
            flags.append("promo_or_noise_title"); base-=.35
        if method=="product-card-variant":
            if not re.fullmatch(r"\d{5}", sku): flags.append("variant_sku_suspicious"); base-=.35
            if not color or not _is_variant_label(color): flags.append("variant_name_missing"); base-=.25
            if title and len(title.split())>10 and _looks_like_marketing_copy(title): flags.append("title_boundary_suspect"); base-=.20
        elif method=="product-card-split":
            base=min(base,0.78)
            if "multicard_split_review" not in flags: flags.append("multicard_split_review")
        elif method=="product-card":
            if re.fullmatch(r"\d+", sku) and not re.fullmatch(r"\d{5}", sku): flags.append("product_sku_suspicious"); base-=.30
        elif method=="order-form-dual-price":
            if not supplier: flags.append("supplier_sku_missing"); base-=.30
            if not title or title==sku: flags.append("weak_title"); base-=.25
            base=max(base,0.92 if supplier and title else base)
        elif method in {"row-price","text-row-price"}:
            # Row/price heuristics are useful for true price lists, but are never allowed a near-perfect score.
            base=min(base,0.84)
            if len(title.split())<2 or re.search(r"^(?:\d+|\d+\s*[=xхв])$", title, re.I):
                flags.append("row_price_weak_title"); base-=.30
        # v1.7.7 text sanity penalties. These flags are created before this quality pass.
        text_gate = {"pdf_cid_glyphs","pdf_symbol_noise","title_symbol_ratio","title_fragment","foreign_sku_in_title","title_too_long","title_empty_after_repair","residual_pdf_punct","body_copy_spill","size_inside_title","fragrance_copy_spill","repeated_title_phrase"}
        present_text_flags=set(flags).intersection(text_gate)
        if present_text_flags:
            base -= min(0.35, 0.12 * len(present_text_flags))
        if clean_text(r.get("dimension_suggestion","")): flags.append("dimension_ocr_corrected")
        # Stable de-duplication keeps Issues Found readable across repeated passes.
        flags=list(dict.fromkeys(flags))
        df.at[i,"quality_confidence"]=round(max(.05,min(.99,base)),3)
        df.at[i,"quality_flags"]=", ".join(flags)
    stats["output_rows"]=len(df); stats["generic_categories"]=int(df["category"].map(lambda x: clean_text(x) in {"", "Visual Catalog"}).sum())
    stats["needs_supplier_sku"]=int(df["quality_flags"].astype(str).str.contains("supplier_sku_missing").sum())
    expected_output=stats["input_rows"]-stats["duplicates_removed"]
    stats["invariant_input_output_mismatch"]=bool(stats["output_rows"] != expected_output)
    stats["invariant_delta"]=int(stats["output_rows"]-expected_output)
    return df, stats

def smart_import_pdf(
    file_obj,
    filename="",
    progress_callback=None,
    chunk_size=100,
    checkpoint_dir=None,
    resume=True,
    return_meta=False,
    visual_ocr=True,
    ocr_dpi=180,
):
    """
    v1.7.9 Catalog Context + Safer Titles: preserves v1.7.7 routing/variants/QA and removes only residual orphan PDF punctuation from recovered titles.

    Routes each PDF page independently to structured-table, text-product, or visual OCR parsing.
    There is no artificial page-count cutoff. Large files are checkpointed in fixed chunks.
    """
    chunk_size = max(10, int(chunk_size or 100))
    file_obj.seek(0)
    data = file_obj.read()
    if checkpoint_dir is None:
        checkpoint_dir = Path.cwd() / "CatalogFix_Checkpoints"
    else:
        checkpoint_dir = Path(checkpoint_dir)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    job_id = _checkpoint_job_id(data, filename)
    job_dir = checkpoint_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = job_dir / "manifest.json"

    fast_reader = PdfReader(io.BytesIO(data))
    total_pages = len(fast_reader.pages)

    # v1.8 whole-document safety gate. Inspect a representative sample before any
    # price heuristics run so engineering graphs/spec tables cannot become products.
    sample_indexes = sorted(set(
        list(range(min(total_pages, 8))) +
        list(range(max(0, total_pages-5), total_pages)) +
        ([total_pages//2] if total_pages else [])
    ))
    sample_texts=[]
    for idx in sample_indexes:
        try:
            sample_texts.append(fast_reader.pages[idx].extract_text() or "")
        except Exception:
            sample_texts.append("")
    doc_safety=_document_type_safety_v18(sample_texts)
    if doc_safety.get("type") in {"technical-datasheet","statistical-report"}:
        # v1.8.15 hybrid protection: a sample may look like a datasheet while a real
        # price list exists elsewhere. Probe pages one-by-one without retaining text.
        hybrid_commercial_page=None
        for probe_idx in range(total_pages):
            try:
                probe_text=fast_reader.pages[probe_idx].extract_text() or ""
            except Exception:
                probe_text=""
            if _page_has_commercial_signal_v19(probe_text):
                hybrid_commercial_page=probe_idx+1
                break
        if hybrid_commercial_page is None:
            dtype=doc_safety.get("type")
            router="TECHNICAL_DATASHEET" if dtype=="technical-datasheet" else "STATISTICAL_REPORT"
            report=pd.DataFrame([{
                "sheet":"DOCUMENT","source_rows":0,"source_columns":0,
                "matrix_products":0,"dimension_products":0,"row_price_products":0,
                "header_products":0,"pattern_products":0,"product_card_products":0,
                "visual_products":0,"router_type":router,"scan_status":f"skipped-{dtype}"
            }])
            meta={
                "job_id":_checkpoint_job_id(data, filename),"checkpoint_folder":"",
                "chunk_size":chunk_size,"total_pages":total_pages,"candidate_pages":0,
                "visual_pages":0,"structured_pages":0,"resumed_chunks":0,
                "document_type":dtype,"document_safety":doc_safety,
                "quality_stats":{"input_rows":0,"output_rows":0,"duplicates_removed":0,
                                 "invariant_input_output_mismatch":False,"invariant_delta":0},
            }
            empty=_dedupe_imported([])
            return (empty, report, meta) if return_meta else (empty, report)
        doc_safety=dict(doc_safety)
        doc_safety["type"]="hybrid-commercial"
        doc_safety["commercial_page_detected"]=hybrid_commercial_page
    manifest = _load_json(manifest_path, {}) if resume else {}
    valid_manifest = (
        manifest.get("job_id") == job_id
        and manifest.get("total_pages") == total_pages
        and int(manifest.get("chunk_size", chunk_size)) == chunk_size
        and str(manifest.get("version", "")) == CHECKPOINT_VERSION
    )
    if not valid_manifest:
        manifest = {
            "version": CHECKPOINT_VERSION, "job_id": job_id, "filename": filename, "file_size": len(data),
            "total_pages": total_pages, "chunk_size": chunk_size, "scan_completed_through": 0,
            "scan_complete": False, "page_routes": {}, "completed_chunks": [],
        }
        _save_json_atomic(manifest_path, manifest)

    page_routes = {int(k): v for k, v in (manifest.get("page_routes", {}) or {}).items()}
    scan_start = int(manifest.get("scan_completed_through", 0)) + 1 if resume else 1
    if not manifest.get("scan_complete", False):
        for i in range(scan_start, total_pages + 1):
            fast_text = fast_reader.pages[i-1].extract_text() or ""
            page_routes[i] = classify_pdf_page_text(fast_text)
            if i % chunk_size == 0 or i == total_pages:
                manifest["scan_completed_through"] = i
                manifest["page_routes"] = {str(k): v for k,v in sorted(page_routes.items())}
                _save_json_atomic(manifest_path, manifest)
            if progress_callback and (i == scan_start or i % 25 == 0 or i == total_pages):
                progress_callback("scan", i, total_pages)
        manifest["scan_complete"] = True
        manifest["scan_completed_through"] = total_pages
        manifest["page_routes"] = {str(k): v for k,v in sorted(page_routes.items())}
        _save_json_atomic(manifest_path, manifest)
    elif progress_callback:
        progress_callback("scan", total_pages, total_pages)

    completed_chunks = set(manifest.get("completed_chunks", [])) if resume else set()
    all_records, page_report = [], []
    resumed_chunks = written_chunks = 0
    for chunk_key in sorted(completed_chunks):
        payload = _load_gzip_json(job_dir / f"chunk_{chunk_key}.json.gz", {}) or {}
        all_records.extend(payload.get("records", []))
        page_report.extend(payload.get("report", []))
        resumed_chunks += 1

    fitz_doc = fitz.open(stream=data, filetype="pdf") if (fitz is not None and visual_ocr) else None
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for chunk_start in range(1, total_pages+1, chunk_size):
            chunk_end = min(total_pages, chunk_start+chunk_size-1)
            chunk_key = f"{chunk_start:06d}_{chunk_end:06d}"
            if chunk_key in completed_chunks:
                if progress_callback: progress_callback("parse", chunk_end, total_pages)
                continue
            chunk_records, chunk_report = [], []
            for page_num in range(chunk_start, chunk_end+1):
                route = page_routes.get(page_num, "TEXT_OTHER")
                page_name = f"PDF p.{page_num}"
                if route == "VISUAL":
                    if not visual_ocr or fitz_doc is None:
                        status="visual-ocr-disabled" if not visual_ocr else "visual-ocr-unavailable"
                        chunk_report.append({"sheet":page_name,"source_rows":0,"source_columns":0,
                            "matrix_products":0,"dimension_products":0,"row_price_products":0,
                            "header_products":0,"pattern_products":0,"visual_products":0,
                            "router_type":"VISUAL","scan_status":status})
                        continue
                    try:
                        recs, rep = extract_visual_catalog_products(fitz_doc, page_num, filename=filename, dpi=ocr_dpi)
                    except Exception as exc:
                        recs = []
                        rep = {"sheet":page_name,"source_rows":0,"source_columns":0,
                            "matrix_products":0,"dimension_products":0,"row_price_products":0,
                            "header_products":0,"pattern_products":0,"visual_products":0,
                            "router_type":"VISUAL","scan_status":f"visual-error: {exc}"}
                    chunk_records.extend(recs); chunk_report.append(rep)
                elif route in {"STRUCTURED_PRICE", "TEXT_PRODUCT"}:
                    page = pdf.pages[page_num-1]
                    page_text = page.extract_text() or ""
                    recs, rep = _parse_pdf_page_v13(page, page_num, page_text, filename=filename)
                    rep["router_type"] = route
                    rep["visual_products"] = 0
                    chunk_records.extend(recs); chunk_report.append(rep)
                else:
                    chunk_report.append({"sheet":page_name,"source_rows":0,"source_columns":0,
                        "matrix_products":0,"dimension_products":0,"row_price_products":0,
                        "header_products":0,"pattern_products":0,"visual_products":0,
                        "router_type":route,"scan_status":"skipped-no-product-signal"})

            _save_gzip_json_atomic(job_dir / f"chunk_{chunk_key}.json.gz", {
                "version":CHECKPOINT_VERSION, "job_id":job_id, "chunk_start":chunk_start, "chunk_end":chunk_end,
                "records":chunk_records, "report":chunk_report,
            })
            completed_chunks.add(chunk_key)
            manifest["completed_chunks"] = sorted(completed_chunks)
            manifest["last_completed_page"] = chunk_end
            manifest["records_saved_so_far"] = int(manifest.get("records_saved_so_far",0)) + len(chunk_records)
            _save_json_atomic(manifest_path, manifest)
            all_records.extend(chunk_records); page_report.extend(chunk_report); written_chunks += 1
            if progress_callback: progress_callback("parse", chunk_end, total_pages)
    if fitz_doc is not None:
        fitz_doc.close()

    imported = _dedupe_imported(all_records)
    suppression_stats={
        "weak_heuristic_rows_suppressed":0,
        "weak_heuristic_suppression_reason":"",
        "suppressed_rows":[],
    }
    # Strong structured order-form extraction may dominate stray heuristic rows from
    # the same document. Suppression is intentionally conservative: relative dominance
    # plus no price plus a clearly weak title/SKU signal.
    if imported is not None and not imported.empty and "import_method" in imported.columns:
        trusted_order_count=int((imported["import_method"].astype(str) == "order-form-dual-price").sum())
        # product_like_rows is the pre-suppression imported row count, including trusted and weak heuristic rows.
        total_product_like_rows=max(1, len(imported))
        dominance_threshold=max(10, int(math.ceil(0.20 * total_product_like_rows)))
        if trusted_order_count >= dominance_threshold:
            weak_methods={"pattern-id","row-price","text-row-price"}
            def _weak_unpriced(r):
                if clean_text(r.get("import_method","")) not in weak_methods:
                    return False
                p=_parse_price_v183(r.get("price",""))
                if not pd.isna(p):
                    return False
                title=clean_text(r.get("title",""))
                sku=clean_text(r.get("sku",""))
                low=title.lower()
                very_short=(len(title) <= 3 or len(title.split()) <= 1)
                noise_word=(norm_header(title) in _BAD_ITEM_WORDS_V181 or norm_header(sku) in _BAD_ITEM_WORDS_V181)
                same_title=bool(title and sku and title == sku)
                title_sanity=_title_sanity_flags_v177(title, sku)
                structural_title_noise=bool(
                    title.count("(") != title.count(")")
                    or title.count("[") != title.count("]")
                    or (re.search(r"[—–]", title) and len(title.split()) >= 5)
                )
                prose_fragment_sku=bool(
                    len(sku) > 8
                    and re.fullmatch(r"[A-Za-z0-9]+", sku)
                    and re.search(r"[A-Za-z]", sku)
                    and re.search(r"\d", sku)
                    and (bool(title_sanity) or structural_title_noise)
                )
                # TODO post-v1.9: move suppression after title repair/polish and tighten
                # GLOBAL_PRODUCT_ID_RE so prose fragments do not enter pattern-id at source.
                return (not title) or same_title or very_short or noise_word or prose_fragment_sku
            drop_idx=[i for i,r in imported.iterrows() if _weak_unpriced(r)]
            if drop_idx:
                suppression_stats["weak_heuristic_rows_suppressed"]=len(drop_idx)
                suppression_stats["weak_heuristic_suppression_reason"]="orderform-dominance"
                suppression_stats["suppressed_rows"]=[
                    {
                        "sku":clean_text(imported.at[i,"sku"]) if "sku" in imported.columns else "",
                        "title":clean_text(imported.at[i,"title"]) if "title" in imported.columns else "",
                        "import_method":clean_text(imported.at[i,"import_method"]) if "import_method" in imported.columns else "",
                        "price":imported.at[i,"price"] if "price" in imported.columns else "",
                        "source_row":imported.at[i,"source_row"] if "source_row" in imported.columns else "",
                    }
                    for i in drop_idx
                ]
                imported=imported.drop(index=drop_idx).reset_index(drop=True)
    imported, recovery_stats = title_recovery_and_multicard_v176(imported)
    # Re-dedupe in case a split card was also discovered independently elsewhere.
    imported = _dedupe_imported(imported.to_dict("records"))
    imported, text_stats = text_sanity_repair_v177(imported)
    imported, polish_stats = final_title_polish_v178(imported)
    imported, quality_stats = quality_intelligence_v17(imported)
    quality_stats.update(recovery_stats)
    quality_stats.update(text_stats)
    quality_stats.update(suppression_stats)
    report = pd.DataFrame(page_report)
    if suppression_stats.get("suppressed_rows"):
        suppressed_df=pd.DataFrame(suppression_stats["suppressed_rows"])
        suppressed_df["sheet"]="Suppressed Rows"
        suppressed_df["router_type"]="QA_SUPPRESSION"
        suppressed_df["scan_status"]="suppressed-orderform-dominance"
        report=pd.concat([report, suppressed_df], ignore_index=True, sort=False)
    route_counts = Counter(page_routes.values())
    meta = {
        "job_id":job_id, "checkpoint_folder":str(job_dir), "chunk_size":chunk_size,
        "total_pages":total_pages, "candidate_pages":sum(1 for v in page_routes.values() if v != "TEXT_OTHER"),
        "visual_pages":route_counts.get("VISUAL",0), "structured_pages":route_counts.get("STRUCTURED_PRICE",0),
        "text_product_pages":route_counts.get("TEXT_PRODUCT",0), "resumed_chunks":resumed_chunks,
        "new_chunks_written":written_chunks, "autosave_enabled":True, "resume_enabled":bool(resume),
        "ocr_enabled":bool(visual_ocr),
        "quality_stats": quality_stats,
    }
    if return_meta: return imported, report, meta
    return imported, report

def standard_import_dataframe(df):
    df = df.copy()
    df.columns = [clean_text(column) for column in df.columns]
    mapping = map_columns(df.columns)
    out = pd.DataFrame(index=df.index)
    for field in CANONICAL_FIELDS:
        source = mapping.get(field)
        out[field] = df[source] if source else ""
    out["source_sheet"] = ""
    out["source_row"] = [i + 2 for i in range(len(out))]
    out["supplier_code"] = ""
    out["import_confidence"] = "HIGH" if len(mapping) >= 3 else "LOW"
    out["import_method"] = "standard-columns"
    return out, mapping


def process_canonical(imported):
    out = imported.copy().reset_index(drop=True)
    for field in CANONICAL_FIELDS:
        if field not in out.columns:
            out[field] = ""

    for field in ["sku", "title", "category", "size", "description"]:
        out[field] = out[field].map(clean_text)
    out["brand"] = out["brand"].map(clean_brand)
    out["color"] = out["color"].map(clean_color)
    out["price"] = out["price"].map(clean_price)
    out["barcode"] = out["barcode"].map(clean_barcode)
    out["sku"] = out["sku"].str.upper().str.replace(r"\s+", "", regex=True)

    for meta in [
        "source_sheet", "source_row", "supplier_code", "import_confidence", "import_method",
        "source_page", "source_table", "matrix_series", "matrix_section", "matrix_model",
        "variant_group", "variant_codes", "currency", "vat_note",
        "attributes_json", "visual_confidence", "router_type",
        "quality_confidence", "quality_flags", "category_source", "dimension_original", "dimension_suggestion"
    ]:
        if meta not in out.columns:
            out[meta] = ""

    issues = []

    def add_issue(index, sku, issue, field, value="", severity="Review"):
        source_row = out.at[index, "source_row"] if "source_row" in out.columns else index + 2
        issues.append({
            "row": int(source_row) if str(source_row).isdigit() else index + 2,
            "sku": sku,
            "severity": severity,
            "field": field,
            "issue": issue,
            "current_value": value,
        })

    for index, row in out.iterrows():
        sku = row["sku"]
        if not sku:
            add_issue(index, sku, "Missing SKU", "sku", "", "Critical")
        if not row["title"]:
            add_issue(index, sku, "Missing product title", "title", "", "Critical")
        if pd.isna(row["price"]):
            add_issue(index, sku, "Missing or invalid price", "price", "", "Critical")
        elif row["price"] < 0:
            add_issue(index, sku, "Negative price", "price", row["price"], "Critical")
        if not row["brand"]:
            add_issue(index, sku, "Missing brand", "brand")
        if not row["category"]:
            add_issue(index, sku, "Missing category", "category")
        try:
            vconf = float(row.get("visual_confidence", "") or 0)
        except Exception:
            vconf = 0
        method_now=clean_text(row.get("import_method", ""))
        if method_now.startswith("visual-") and vconf and vconf < 0.60:
            add_issue(index, sku, "Low OCR confidence — verify visual extraction", "sku", sku, "Gate")

        # v1.7.8 export gate: uncertain parser output must not silently enter Shopify Ready.
        method = clean_text(row.get("import_method", ""))
        title_low = clean_text(row.get("title", "")).lower()
        color = clean_text(row.get("color", ""))
        qflags = clean_text(row.get("quality_flags", ""))
        try:
            qconf = float(row.get("quality_confidence", "") or 0)
        except Exception:
            qconf = 0.0
        gate_flags = {"weak_title", "promo_or_noise_title", "variant_sku_suspicious", "variant_name_missing", "title_boundary_suspect", "row_price_weak_title", "product_sku_suspicious", "multicard_split_review", "pdf_cid_glyphs", "pdf_symbol_noise", "title_symbol_ratio", "title_fragment", "foreign_sku_in_title", "title_too_long", "title_empty_after_repair", "residual_pdf_punct", "body_copy_spill", "size_inside_title", "fragrance_copy_spill", "repeated_title_phrase", "supplier_sku_missing"}
        present_flags = {x.strip() for x in qflags.split(",") if x.strip()}
        if method in {"standard-commercial-table","commercial-text-row","order-form-dual-price","vehicle-price-row"}:
            sanity_now = {x for x in _title_sanity_flags_v177(row.get("title", ""), sku)
                          if x in {"pdf_cid_glyphs","pdf_symbol_noise","title_empty_after_repair","repeated_title_phrase"}}
        else:
            sanity_now = set(_title_sanity_flags_v177(row.get("title", ""), sku))
        present_flags.update(sanity_now)
        if qconf and qconf < 0.72:
            add_issue(index, sku, "Low quality confidence — verify product card", "quality_confidence", qconf, "Gate")
        if gate_flags.intersection(present_flags):
            blocked = ", ".join(sorted(gate_flags.intersection(present_flags)))
            add_issue(index, sku, "Quality gate blocked uncertain extraction", "quality_flags", blocked or qflags, "Gate")
        if any(x in title_low for x in _PROMO_PHRASES) or re.search(r"\b(?:подробнее|скидка|закажи)\b", title_low):
            add_issue(index, sku, "Promotional text detected in product title", "title", row.get("title", ""), "Gate")
        if method == "product-card-variant":
            if not re.fullmatch(r"\d{5}", sku):
                add_issue(index, sku, "Variant SKU is not a verified 5-digit catalogue code", "sku", sku, "Gate")
            if not color or not _is_variant_label(color):
                add_issue(index, sku, "Variant name/color is missing or suspicious", "color", color, "Gate")
        if method == "product-card-split":
            add_issue(index, sku, "Side-by-side product card was split automatically — verify title/price", "title", row.get("title", ""), "Gate")
        if method.startswith("product-card") and clean_text(row.get("title", "")) == sku:
            add_issue(index, sku, "Product title could not be recovered from card context", "title", row.get("title", ""), "Gate")
        if method in {"row-price", "text-row-price"} and (len(clean_text(row.get("title", "")).split()) < 2):
            add_issue(index, sku, "Weak row-price match — verify against source", "title", row.get("title", ""), "Gate")

        barcode = row["barcode"]
        if barcode and not re.fullmatch(r"[A-Za-z0-9\-]+", barcode):
            add_issue(index, sku, "Barcode contains unusual characters", "barcode", barcode)

    duplicate_sku_mask = out["sku"].ne("") & out["sku"].duplicated(keep=False)
    for index in out.index[duplicate_sku_mask]:
        add_issue(index, out.at[index, "sku"], "Duplicate SKU", "sku", out.at[index, "sku"], "Critical")

    signature_fields = ["title", "brand", "size", "color"]
    signature = out[signature_fields].astype(str).apply(
        lambda row: "|".join(value.lower() for value in row), axis=1
    )
    duplicate_signature_mask = signature.ne("|||") & signature.duplicated(keep=False)
    for index in out.index[duplicate_signature_mask]:
        add_issue(index, out.at[index, "sku"], "Possible duplicate product/variant", "product", signature.at[index])

    issues_df = pd.DataFrame(
        issues,
        columns=["row", "sku", "severity", "field", "issue", "current_value"],
    )

    critical_indices = set()
    if not issues_df.empty:
        critical_skus = set(issues_df.loc[issues_df["severity"].isin(["Critical", "Gate"]), "sku"].astype(str))
        for idx, sku in out["sku"].items():
            if str(sku) in critical_skus or not sku:
                critical_indices.add(idx)

    out["qa_status"] = ["NEEDS REVIEW" if i in critical_indices else "READY" for i in out.index]

    shop_all = pd.DataFrame(index=out.index)
    shop_all["Handle"] = [
        slugify(row["title"], row["sku"] or f"row-{index + 2}")
        for index, row in out.iterrows()
    ]
    shop_all["Title"] = out["title"]
    shop_all["Body (HTML)"] = out["description"]
    shop_all["Vendor"] = out["brand"]
    shop_all["Product Category"] = out["category"]
    shop_all["Type"] = out["category"]
    shop_all["Variant SKU"] = out["sku"]
    shop_all["Variant Price"] = out["price"]
    shop_all["Variant Barcode"] = out["barcode"]
    shop_all["Option1 Name"] = out["size"].map(lambda value: "Size" if value else "")
    shop_all["Option1 Value"] = out["size"]
    shop_all["Option2 Name"] = out["color"].map(lambda value: "Color" if value else "")
    shop_all["Option2 Value"] = out["color"]
    shop_all["Status"] = "draft"

    ready_mask = out["qa_status"] == "READY"
    shop_ready = shop_all.loc[ready_mask].reset_index(drop=True)
    needs_review = out.loc[~ready_mask].reset_index(drop=True)
    return out, issues_df, shop_ready, needs_review


def to_excel_bytes(cleaned, issues, shop_ready, needs_review, import_report=None):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        cleaned.to_excel(writer, sheet_name="Clean Master", index=False)
        issues.to_excel(writer, sheet_name="Issues Found", index=False)
        shop_ready.to_excel(writer, sheet_name="Shopify Ready", index=False)
        needs_review.to_excel(writer, sheet_name="Needs Review", index=False)
        if import_report is not None and not import_report.empty:
            import_report.to_excel(writer, sheet_name="Import Report", index=False)
    buffer.seek(0)
    return buffer.getvalue()