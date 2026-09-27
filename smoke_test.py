import io
import pathlib
import py_compile
import sys
import tempfile

FILES = ("app.py", "catalogfix_core.py")

for name in FILES:
    path = pathlib.Path(name)
    if not path.exists():
        print(f"SMOKE FAIL: missing {name}")
        sys.exit(1)
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        print(f"SYNTAX FAIL: {name}: {exc}")
        sys.exit(1)

try:
    import catalogfix_core
    import fitz
    from pypdf import PdfWriter
except Exception as exc:
    print(f"IMPORT FAIL: {type(exc).__name__}: {exc}")
    sys.exit(1)

# Visual SKU prefilter/normalization regression: explicit separators may contain
# OCR spacing and one-digit suffixes are allowed only with an explicit '-'/'_'.
try:
    visual_positive = {
        "DW - 01": "DW-01",
        "DW-02": "DW-02",
        "DW - 03": "DW-03",
        "DW_04": "DW-04",
        "MWO-1": "MWO-1",
        "MWO-2": "MWO-2",
        "FSCR01": "FSCR01",
        "FSCR 01": "FSCR01",
        "BIO-01": "BIO-01",
        "CW 165": "CW-165",
        "CW 46": "CW-46",
    }
    for raw, expected in visual_positive.items():
        got = catalogfix_core._visual_codes_from_text(raw)
        if expected not in got:
            raise AssertionError(f"VISUAL SKU REGRESSION: {raw!r} -> {got}, expected {expected!r}")

    for raw in ("MODEL1", "the 12", "ITEM 01", "FIG 3", "PAGE 12", "RAILING"):
        got = catalogfix_core._visual_codes_from_text(raw)
        if got:
            raise AssertionError(f"VISUAL SKU NEGATIVE REGRESSION: {raw!r} -> {got}")
except Exception as exc:
    print(f"VISUAL SKU REGRESSION FAIL: {type(exc).__name__}: {exc}")
    sys.exit(1)

# B4-v1 lane boundary freeze: association uses normalized center-x only.
# The exact boundary semantics are LEFT for cx < 0.50 and RIGHT for cx >= 0.50.
try:
    assert catalogfix_core._visual_lane(0.49) == "LEFT"
    assert catalogfix_core._visual_lane(0.50) == "RIGHT"
    assert catalogfix_core._visual_lane(0.51) == "RIGHT"
    # p.21 observed 55,690/- candidate center-x.
    assert catalogfix_core._visual_lane(0.427) == "LEFT"
except Exception as exc:
    print(f"B4 LANE REGRESSION FAIL: {type(exc).__name__}: {exc}")
    sys.exit(1)

# B4-v1 ownership interval freeze: sampled geometry + explicit boundaries.
try:
    # p.11 RIGHT: non-last owner via [sku.cy, next_sku.cy)
    assert catalogfix_core._visual_owner(0.421, [
        {"code": "BRAVO60", "cy": 0.112},
        {"code": "BRAVO78", "cy": 0.557},
    ]) == "BRAVO60"

    # p.21 LEFT: non-last owner via [sku.cy, next_sku.cy)
    assert catalogfix_core._visual_owner(0.393, [
        {"code": "BIO-01", "cy": 0.112},
        {"code": "BIO-02", "cy": 0.557},
    ]) == "BIO-01"

    # Observed last-SKU eligible candidates within the conservative 0.445 cap.
    assert catalogfix_core._visual_owner(
        0.894, [{"code": "BRAVO60", "cy": 0.557}]
    ) == "BRAVO60"
    assert catalogfix_core._visual_owner(
        0.895, [{"code": "BRAVO78", "cy": 0.557}]
    ) == "BRAVO78"
    assert catalogfix_core._visual_owner(
        0.894, [{"code": "BIO-02", "cy": 0.557}]
    ) == "BIO-02"

    # Candidate exactly at the next SKU center belongs to that next SKU.
    assert catalogfix_core._visual_owner(0.557, [
        {"code": "BRAVO60", "cy": 0.112},
        {"code": "BRAVO78", "cy": 0.557},
    ]) == "BRAVO78"

    # Beyond the last-SKU max depth: not owned.
    assert catalogfix_core._visual_owner(
        1.01, [{"code": "BRAVO78", "cy": 0.557}]
    ) is None

    # p.21 RIGHT geometry: no eligible was observed below IN-590, but the
    # ownership interval remains open until cy + 0.445.
    assert catalogfix_core._visual_owner(
        1.0, [{"code": "IN-590", "cy": 0.829}]
    ) == "IN-590"
    assert catalogfix_core._visual_owner(
        1.3, [{"code": "IN-590", "cy": 0.829}]
    ) is None
except Exception as exc:
    print(f"B4 OWNERSHIP REGRESSION FAIL: {type(exc).__name__}: {exc}")
    sys.exit(1)

# B4-v1 physical OCR collision detector freeze. Fixtures use the sampled
# 2977x2105 page size to convert raw probe boxes into normalized page space.
try:
    def _b4_box(text, bbox, width=2977.0, height=2105.0):
        x1, y1, x2, y2 = bbox
        return {
            "text": text,
            "bbox": bbox,
            "normalized_bbox": [
                x1 / width, y1 / height, x2 / width, y2 / height
            ],
        }

    # p.11 BRAVO60: same parsed code from two distinct physical OCR boxes.
    bravo_collision = [
        _b4_box("Bravo 60-4", [1626.5, 210.8, 1901.1, 260.1]),
        _b4_box("Bravo 60-3", [136.5, 1148.2, 411.1, 1197.5]),
    ]
    assert catalogfix_core._visual_collision_codes(bravo_collision) == {
        "BRAVO60": 2
    }

    # Same code, same physical box (retry/duplicate): one physical hit, not collision.
    same_box_retry = [
        _b4_box("Bio-01", [141.0, 210.8, 298.6, 261.6]),
        _b4_box("Bio-01", [141.0, 210.8, 298.6, 261.6]),
    ]
    assert catalogfix_core._visual_collision_codes(same_box_retry) == {}

    # p.9 note: multiple parsed codes from one physical box are B3-B, not a
    # collision by themselves. Commit 5 intentionally adds no SKU blacklist.

    # p.21 clean page: distinct codes from distinct boxes produce no collision.
    clean_page = [
        _b4_box("Bio-01", [141.0, 210.8, 298.6, 261.6]),
        _b4_box("Bio-05", [1626.5, 210.8, 1823.1, 261.6]),
        _b4_box("Bio-02", [141.0, 1146.7, 310.6, 1199.0]),
    ]
    assert catalogfix_core._visual_collision_codes(clean_page) == {}
except Exception as exc:
    print(f"B4 COLLISION REGRESSION FAIL: {type(exc).__name__}: {exc}")
    sys.exit(1)

# B4-v1 price-association eligibility regression. These fixtures are the exact
# token classes validated by the targeted harness/probe; no production wiring
# exists yet in this commit.
try:
    eligibility_cases = {
        "23,990/-": "ELIGIBLE",
        "24,990/-": "ELIGIBLE",
        "49,490/-": "ELIGIBLE",
        "26,990/-": "ELIGIBLE",
        "55,690/-": "ELIGIBLE",
        "600 × 520 mm": "DIMENSION_REJECT",
        "555 × 475 mm": "DIMENSION_REJECT",
        "685 × 405 mm": "DIMENSION_REJECT",
        "595 × 595 X 555 mm": "DIMENSION_REJECT",
        "598 × 598 × 555 mm": "DIMENSION_REJECT",
        "-/066'66": "MALFORMED_REJECT",
    }
    for raw, expected in eligibility_cases.items():
        got = catalogfix_core._visual_association_price_classification_v1(raw)
        if got != expected:
            raise AssertionError(
                f"B4 ELIGIBILITY REGRESSION: {raw!r} -> {got!r}, expected {expected!r}"
            )
except Exception as exc:
    print(f"B4 ELIGIBILITY REGRESSION FAIL: {type(exc).__name__}: {exc}")
    sys.exit(1)

# Text/no-OCR route: exercises checkpoint setup, routing and return_meta plumbing.
try:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    with tempfile.TemporaryDirectory() as tmp:
        imported, report, meta = catalogfix_core.smart_import_pdf(
            buf,
            filename="smoke-no-ocr.pdf",
            checkpoint_dir=tmp,
            resume=False,
            return_meta=True,
            visual_ocr=False,
        )
    assert meta["total_pages"] == 1
    assert imported is not None
    assert report is not None
except Exception as exc:
    print(f"PDF SMOKE FAIL: {type(exc).__name__}: {exc}")
    sys.exit(1)

# Visual/OCR route: create a one-page PDF with no text layer and let the production
# PyMuPDF + RapidOCR path execute. It may find zero products, but must not error.
try:
    doc = fitz.open()
    doc.new_page(width=200, height=200)
    visual_bytes = doc.tobytes()
    doc.close()
    visual_buf = io.BytesIO(visual_bytes)
    with tempfile.TemporaryDirectory() as tmp:
        imported_v, report_v, meta_v = catalogfix_core.smart_import_pdf(
            visual_buf,
            filename="smoke-visual.pdf",
            checkpoint_dir=tmp,
            resume=False,
            return_meta=True,
            visual_ocr=True,
            ocr_dpi=100,
        )
    assert meta_v["total_pages"] == 1
    assert meta_v.get("visual_pages", 0) == 1
    if report_v is not None and not report_v.empty and "scan_status" in report_v.columns:
        statuses = " ".join(report_v["scan_status"].astype(str).tolist())
        assert "visual-error" not in statuses
        assert "visual-ocr-unavailable" not in statuses
except Exception as exc:
    print(f"VISUAL PDF SMOKE FAIL: {type(exc).__name__}: {exc}")
    sys.exit(1)

# Structured order-form route: generate enough trusted rows to exercise the
# order-form dominance threshold and its math/quality plumbing.
try:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    # Coordinates intentionally sit inside the current relative zones used by
    # extract_order_form_products_v181 (IMSAI regression layout), not a universal A4 grid.
    page.insert_text((45, 55), "IMSAI ORDER FORM", fontsize=12)
    page.insert_text((125, 85), "ITEM NO", fontsize=8)
    page.insert_text((220, 85), "DESCRIPTION", fontsize=8)
    page.insert_text((480, 85), "KIT PRICE", fontsize=8)
    page.insert_text((540, 85), "ASSEMBLED PRICE", fontsize=7)
    page.insert_text((220, 105), "MEMORY EXPANSION", fontsize=9)
    y = 130
    for n in range(1, 13):
        page.insert_text((130, y), f"T{n:02d}", fontsize=8)
        page.insert_text((220, y), f"TEST PRODUCT {n}", fontsize=8)
        page.insert_text((485, y), f"$ {100+n:.2f}", fontsize=8)
        page.insert_text((545, y), f"$ {200+n:.2f}", fontsize=8)
        y += 18
    order_bytes = doc.tobytes()
    doc.close()
    order_buf = io.BytesIO(order_bytes)
    with tempfile.TemporaryDirectory() as tmp:
        imported_o, report_o, meta_o = catalogfix_core.smart_import_pdf(
            order_buf,
            filename="smoke-order-form.pdf",
            checkpoint_dir=tmp,
            resume=False,
            return_meta=True,
            visual_ocr=False,
        )
    if imported_o is None or imported_o.empty:
        raise AssertionError("ORDER-FORM SMOKE: no products parsed")
    methods = set(imported_o["import_method"].astype(str))
    if methods != {"order-form-dual-price"}:
        raise AssertionError(f"ORDER-FORM SMOKE: wrong methods {sorted(methods)}")
    if len(imported_o) != 12:
        raise AssertionError(f"ORDER-FORM SMOKE: expected 12 rows, got {len(imported_o)}")
    if "quality_stats" not in meta_o:
        raise AssertionError("ORDER-FORM SMOKE: quality_stats missing")
except Exception as exc:
    print(f"ORDER-FORM SMOKE FAIL: {type(exc).__name__}: {exc}")
    sys.exit(1)

print("OK: syntax + import + smart_import_pdf + visual OCR route + order-form route")
