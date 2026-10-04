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

# B4-v1 commit-6 wiring freeze: selected-pass normalization, supplier-only
# mutation, same-lane/same-cy skip, collision skip, associated and ambiguous.
try:
    def _norm_box(text, bbox, score=0.99, width=1000.0, height=1000.0):
        raw = {"text": text, "score": score, "bbox": bbox}
        return catalogfix_core._normalize_boxes([raw], (height, width, 3))[0]

    def _supplier(code, bbox):
        return {
            "supplier_code": code,
            "sku": code,
            "price": None,
            "attributes_json": json.dumps({
                "sku_bbox": bbox,
                "price_source": "missing",
                "keep": "same",
            }),
            "visual_confidence": 0.99,
            "quality_confidence": 0.94,
            "quality_flags": "generic_category",
        }

    # BRAVO60-style collision is skipped; unrelated derived fields stay untouched.
    collision_records = [_supplier("BRAVO60", [550, 100, 650, 120])]
    collision_boxes = [
        _norm_box("Bravo 60-4", [550, 100, 650, 120]),
        _norm_box("Bravo 60-3", [50, 550, 150, 570]),
        _norm_box("24,990/-", [600, 850, 700, 870]),
    ]
    before_derived = (
        collision_records[0]["visual_confidence"],
        collision_records[0]["quality_confidence"],
        collision_records[0]["quality_flags"],
    )
    catalogfix_core._b4_associate(
        collision_records, collision_boxes, {"BRAVO60": 2},
        [{"text": b["text"], "score": b["score"], "bbox": b["bbox"]} for b in collision_boxes],
        (1000, 1000, 3),
    )
    assert collision_records[0]["price"] is None
    assert json.loads(collision_records[0]["attributes_json"])["price_source"] == "visual-association-skipped"
    assert before_derived == (
        collision_records[0]["visual_confidence"],
        collision_records[0]["quality_confidence"],
        collision_records[0]["quality_flags"],
    )

    # Same cy in different lanes is valid; same lane + same cy skips both.
    lane_records = [
        _supplier("BIO-01", [50, 100, 150, 120]),
        _supplier("BIO-05", [550, 100, 650, 120]),
    ]
    lane_boxes = [
        _norm_box("Bio-01", [50, 100, 150, 120]),
        _norm_box("Bio-05", [550, 100, 650, 120]),
        _norm_box("55,690/-", [200, 350, 300, 370]),
    ]
    catalogfix_core._b4_associate(
        lane_records, lane_boxes, {},
        [{"text": b["text"], "score": b["score"], "bbox": b["bbox"]} for b in lane_boxes],
        (1000, 1000, 3),
    )
    assert lane_records[0]["price"] == 55690.0
    assert json.loads(lane_records[0]["attributes_json"])["price_source"] == "visual-associated"

    same_lane_records = [
        _supplier("MIN-300", [550, 100, 650, 120]),
        _supplier("MAX-580", [650, 100, 750, 120]),
    ]
    same_lane_boxes = [
        _norm_box("Min 300", [550, 100, 650, 120]),
        _norm_box("Max 580", [650, 100, 750, 120]),
    ]
    catalogfix_core._b4_associate(
        same_lane_records, same_lane_boxes, {},
        [{"text": b["text"], "score": b["score"], "bbox": b["bbox"]} for b in same_lane_boxes],
        (1000, 1000, 3),
    )
    assert all(
        json.loads(r["attributes_json"])["price_source"] == "visual-association-skipped"
        for r in same_lane_records
    )

    # Zero owned eligible prices is a distinct B4-emitted protected null.
    none_records = [_supplier("BIO-02", [50, 550, 150, 570])]
    none_boxes = [
        _norm_box("Bio-02", [50, 550, 150, 570]),
        _norm_box("600 × 520 mm", [200, 850, 300, 870]),
    ]
    catalogfix_core._b4_associate(
        none_records, none_boxes, {},
        [{"text": b["text"], "score": b["score"], "bbox": b["bbox"]} for b in none_boxes],
        (1000, 1000, 3),
    )
    assert none_records[0]["price"] is None
    assert json.loads(none_records[0]["attributes_json"])["price_source"] == "visual-associated-none"

    # Two eligible owned prices fail closed as ambiguous.
    ambiguous_records = [_supplier("BIO-01", [50, 100, 150, 120])]
    ambiguous_boxes = [
        _norm_box("Bio-01", [50, 100, 150, 120]),
        _norm_box("49,490/-", [200, 300, 300, 320]),
        _norm_box("55,690/-", [200, 400, 300, 420]),
    ]
    catalogfix_core._b4_associate(
        ambiguous_records, ambiguous_boxes, {},
        [{"text": b["text"], "score": b["score"], "bbox": b["bbox"]} for b in ambiguous_boxes],
        (1000, 1000, 3),
    )
    amb = json.loads(ambiguous_records[0]["attributes_json"])
    assert ambiguous_records[0]["price"] is None
    assert amb["price_source"] == "visual-ambiguous"
    assert amb["price_candidate_count"] == 2
    assert amb["price_candidate_texts"] == ["49,490/-", "55,690/-"]

    # Non-supplier card records are outside B4 mutation scope.
    card = {"supplier_code": "", "price": None, "attributes_json": "{\"keep\": true}"}
    catalogfix_core._b4_associate([card], [], {}, [], (1000, 1000, 3))
    assert card == {"supplier_code": "", "price": None, "attributes_json": "{\"keep\": true}"}
except Exception as exc:
    print(f"B4 WIRING REGRESSION FAIL: {type(exc).__name__}: {exc}")
    sys.exit(1)

# B4->dedupe integration contract: protected B4 nulls stay null even when
# duplicate-SKU records carry a price. Non-protected missing continues to enrich.
try:
    def _dedupe_fixture(sku, source, price, title, source_row):
        return {
            "sku": sku,
            "title": title,
            "brand": "Test",
            "price": price,
            "category": "Visual Catalog",
            "size": "",
            "color": "",
            "description": title,
            "barcode": "",
            "source_sheet": "PDF p.1",
            "source_row": source_row,
            "supplier_code": sku,
            "import_confidence": "HIGH",
            "import_method": "visual-high-intelligence",
            "source_page": 1,
            "source_table": "visual-layout-page",
            "matrix_series": "Visual Catalog",
            "matrix_section": "Visual catalog",
            "matrix_model": sku,
            "variant_group": title,
            "variant_codes": sku,
            "currency": "",
            "vat_note": "",
            "attributes_json": json.dumps({"price_source": source, "keep": "base"}),
            "visual_confidence": 0.99,
            "router_type": "VISUAL_HI",
            "quality_confidence": 0.94,
            "quality_flags": "generic_category",
            "category_source": "",
            "dimension_original": "",
            "dimension_suggestion": "",
        }

    for protected_source in [
        "visual-ambiguous",
        "visual-association-skipped",
        "visual-associated-none",
    ]:
        protected_base = _dedupe_fixture(
            f"PROTECTED-{protected_source}", protected_source, None,
            "Long protected base title", 1,
        )
        priced_duplicate = _dedupe_fixture(
            f"PROTECTED-{protected_source}", "visual-associated", 46990,
            "Short", 2,
        )
        merged = catalogfix_core._dedupe_imported([protected_base, priced_duplicate])
        assert len(merged) == 1
        assert merged.iloc[0]["price"] is None
        attrs = json.loads(merged.iloc[0]["attributes_json"])
        assert attrs["price_source"] == protected_source
        assert attrs["keep"] == "base"

    ordinary_missing = _dedupe_fixture(
        "ORDINARY-MISSING", "missing", None, "Long ordinary base title", 1
    )
    ordinary_priced = _dedupe_fixture(
        "ORDINARY-MISSING", "visual-associated", 12345, "Short", 2
    )
    merged_missing = catalogfix_core._dedupe_imported([ordinary_missing, ordinary_priced])
    assert merged_missing.iloc[0]["price"] == 12345
    assert json.loads(merged_missing.iloc[0]["attributes_json"])["price_source"] == "missing"

    associated_base = _dedupe_fixture(
        "ASSOCIATED-KEEP", "visual-associated", 27990, "Long associated base title", 1
    )
    associated_other = _dedupe_fixture(
        "ASSOCIATED-KEEP", "visual-associated", 24990, "Short", 2
    )
    merged_associated = catalogfix_core._dedupe_imported([associated_base, associated_other])
    assert merged_associated.iloc[0]["price"] == 27990
    assert json.loads(merged_associated.iloc[0]["attributes_json"])["price_source"] == "visual-associated"
except Exception as exc:
    print(f"B4 DEDUPE CONTRACT FAIL: {type(exc).__name__}: {exc}")
    sys.exit(1)

# B3-B diagnostic lexical candidate classifier. Production extraction is not
# wired to this helper yet; these fixtures freeze only the preregistered classes.
try:
    b3b_reject_cases = [
        ("Upto 1350 m3/hr", "UPTO1350", "SEMANTIC_PREFIX_REJECT"),
        ("Upto 1250 m3/hr", "UPTO1250", "SEMANTIC_PREFIX_REJECT"),
        ("Min 300, Max 580", "MIN-300", "SEMANTIC_PREFIX_REJECT"),
        ("Min 300, Max 580", "MAX-580", "SEMANTIC_PREFIX_REJECT"),
        ("Price 60 cm", "PRICE60", "SEMANTIC_PREFIX_REJECT"),
        ("Price 75 cm", "PRICE75", "SEMANTIC_PREFIX_REJECT"),
        ("Shop 43, Ground floor, Atria Mall", "SHOP43", "ADDRESS_CONTEXT_REJECT"),
        ("SCO 298, Sector - 29, Gurugram", "SCO-298", "ADDRESS_CONTEXT_REJECT"),
        ("Showroom no -6, Binori B Square iii", "NO-6", "ADDRESS_CONTEXT_REJECT"),
        ("Tangential fan 30-70°C adjustable thermostat", "FAN-30", "TEMPERATURE_CONTEXT_REJECT"),
        ("Temp. Range Up to 60°C", "TO-60", "SEMANTIC_PHRASE_REJECT"),
    ]
    for raw, code, expected in b3b_reject_cases:
        got = catalogfix_core._visual_code_candidate_classification_v1(raw, code)
        if got != expected:
            raise AssertionError(f"B3-B CLASSIFIER: {raw!r}, {code!r} -> {got!r}, expected {expected!r}")

    b3b_accept_cases = [
        ("Bravo 60-4", "BRAVO60"),
        ("Bravo 78-3", "BRAVO78"),
        ("Bio - 05", "BIO-05"),
        ("MWO-1", "MWO-1"),
        ("FSCR01", "FSCR01"),
        ("CIGAR212", "CIGAR212"),
    ]
    for raw, code in b3b_accept_cases:
        got = catalogfix_core._visual_code_candidate_classification_v1(raw, code)
        if got != "ACCEPT":
            raise AssertionError(f"B3-B ACCEPT REGRESSION: {raw!r}, {code!r} -> {got!r}")
except Exception as exc:
    print(f"B3-B CLASSIFIER REGRESSION FAIL: {type(exc).__name__}: {exc}")
    sys.exit(1)

# Multi-page SKU price policy v1: same SKU + conflicting non-null prices
# across different source pages must fail closed; same price or one priced page is safe.
try:
    import pandas as _pd

    conflict_group = _pd.DataFrame([
        {"sku":"SKU-X","price":100,"source_page":1,"source_row":1},
        {"sku":"SKU-X","price":120,"source_page":2,"source_row":2},
    ])
    got = catalogfix_core._multi_page_price_conflict_v1(conflict_group)
    if not got or got["prices"] != [100.0, 120.0]:
        raise AssertionError(f"MULTI-PAGE CONFLICT DETECTION: {got!r}")

    same_price_group = _pd.DataFrame([
        {"sku":"SKU-X","price":100,"source_page":1,"source_row":1},
        {"sku":"SKU-X","price":100,"source_page":2,"source_row":2},
    ])
    if catalogfix_core._multi_page_price_conflict_v1(same_price_group) is not None:
        raise AssertionError("MULTI-PAGE SAME PRICE should not conflict")

    one_price_group = _pd.DataFrame([
        {"sku":"SKU-X","price":100,"source_page":1,"source_row":1},
        {"sku":"SKU-X","price":None,"source_page":2,"source_row":2},
    ])
    if catalogfix_core._multi_page_price_conflict_v1(one_price_group) is not None:
        raise AssertionError("MULTI-PAGE ONE PRICE should not conflict")

    deduped = catalogfix_core._dedupe_imported([
        {"sku":"SKU-X","title":"A","price":100,"source_page":1,"source_row":1,"import_method":"visual-high-intelligence","attributes_json":"{}"},
        {"sku":"SKU-X","title":"A","price":120,"source_page":2,"source_row":2,"import_method":"visual-high-intelligence","attributes_json":"{}"},
    ])
    row = deduped.iloc[0]
    if row["price"] is not None:
        raise AssertionError(f"MULTI-PAGE FAIL-CLOSED price={row['price']!r}")
    attrs = json.loads(row["attributes_json"])
    if attrs.get("price_source") != "multi-page-conflict":
        raise AssertionError(f"MULTI-PAGE source={attrs!r}")
except Exception as exc:
    print(f"MULTI-PAGE POLICY REGRESSION FAIL: {type(exc).__name__}: {exc}")
    sys.exit(1)

# B3-B record-context regression: reject false codes whose meaning is only
# recoverable after nearby visual context is known.
try:
    b3b_record_context_cases = [
        (("FAN-30", "adjustable thermostat"), "TEMPERATURE_CONTEXT_REJECT"),
        (("FAN-30", "Tangential fan"), "ACCEPT"),
        (("IN-590", "Product Dimension"), "DIMENSION_CONTEXT_REJECT"),
        (("IN-590", "CARYSIL"), "ACCEPT"),
        (("MWO-1", "Product Dimension"), "ACCEPT"),
    ]
    for args, expected in b3b_record_context_cases:
        got = catalogfix_core._visual_code_record_context_classification_v1(*args)
        if got != expected:
            raise AssertionError(f"B3-B RECORD CONTEXT: {args!r} -> {got!r}, expected {expected!r}")
except Exception as exc:
    print(f"B3-B RECORD CONTEXT REGRESSION FAIL: {type(exc).__name__}: {exc}")
    sys.exit(1)

# B3-B production wiring regression: _visual_codes_from_text must now apply
# the gated candidate classifier while preserving accepted supplier-code forms.
try:
    b3b_wiring_reject = {
        "Upto 1350 m3/hr": [],
        "Price 75 cm": [],
        "Min 300, Max 580": [],
        "Shop 43, Ground floor, Atria Mall": [],
        "SCO 298, Sector - 29, Gurugram": [],
        "Showroom no -6, Binori B Square iii": [],
        "Tangential fan 30-70°C adjustable thermostat": [],
        "Temp. Range Up to 60°C": [],
    }
    for raw, expected in b3b_wiring_reject.items():
        got = catalogfix_core._visual_codes_from_text(raw)
        if got != expected:
            raise AssertionError(f"B3-B WIRING REJECT: {raw!r} -> {got!r}, expected {expected!r}")

    b3b_wiring_accept = {
        "Bravo 60-4": ["BRAVO60"],
        "Bravo 78-3": ["BRAVO78"],
        "Bio - 05": ["BIO-05"],
        "MWO-1": ["MWO-1"],
        "FSCR01": ["FSCR01"],
        "CIGAR212": ["CIGAR212"],
    }
    for raw, expected in b3b_wiring_accept.items():
        got = catalogfix_core._visual_codes_from_text(raw)
        if got != expected:
            raise AssertionError(f"B3-B WIRING ACCEPT: {raw!r} -> {got!r}, expected {expected!r}")
except Exception as exc:
    print(f"B3-B WIRING REGRESSION FAIL: {type(exc).__name__}: {exc}")
    sys.exit(1)

# B4-v1 price-association eligibility regression. These fixtures are the exact
# token classes validated by the targeted harness/probe; production wiring
# is exercised separately above.
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
