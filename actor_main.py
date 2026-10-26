import asyncio
import io
import json
import mimetypes
import os
os.environ['CATALOGFIX_TEXT_ONLY_RUNTIME']='true'
import re
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd
from apify import Actor
from pypdf import PdfReader

from catalogfix_core import (
    CANONICAL_FIELDS,
    RELEASE_VERSION,
    map_columns,
    process_canonical,
    smart_import_excel,
    smart_import_pdf,
    standard_import_dataframe,
    to_excel_bytes,
)

_ALLOWED_EXTENSIONS = {".pdf", ".csv", ".xlsx", ".xls"}



def _catalog_size_tier(data: bytes, filename: str) -> tuple[str, int | None]:
    """Return billing tier and PDF page count when applicable."""
    if filename.lower().endswith(".pdf"):
        pages = len(PdfReader(io.BytesIO(data)).pages)
        if pages > 500:
            raise ValueError(
                f"This Store version supports PDF catalogs up to 500 pages; received {pages}. "
                "Split the catalog or contact the provider for a managed run."
            )
        if pages <= 50:
            return "catalog-small", pages
        if pages <= 200:
            return "catalog-medium", pages
        return "catalog-large", pages
    return "catalog-small", None


async def _charge_if_ppe(event_name: str) -> None:
    """Charge only when this Actor is currently running under PPE pricing."""
    try:
        pricing = Actor.get_charging_manager().get_pricing_info()
        if getattr(pricing, "is_pay_per_event", False):
            await Actor.charge(event_name=event_name)
    except Exception as exc:
        Actor.log.warning("PPE charge skipped/failed safely: %s", exc)

async def _download_input(source: str, filename_hint: str = "") -> tuple[bytes, str]:
    source = str(source or "").strip()
    if not source:
        raise ValueError("catalogFile is required.")

    if not re.match(r"^https?://", source, re.I):
        raise ValueError(
            "catalogFile must resolve to an HTTP(S) URL. "
            "Use the Apify file-upload field or paste a direct file URL."
        )

    content_type = ""
    parsed_source = urllib.parse.urlparse(source)
    m = re.fullmatch(r"/v2/key-value-stores/([^/]+)/records/(.+)", parsed_source.path)
    if parsed_source.netloc.lower() == "api.apify.com" and m:
        store_id = urllib.parse.unquote(m.group(1))
        record_key = urllib.parse.unquote(m.group(2))
        record = await Actor.apify_client.key_value_store(store_id).get_record_as_bytes(record_key)
        if not record:
            raise ValueError("Uploaded Apify file record could not be read.")
        data = record.get("value", b"")
        content_type = record.get("content_type") or ""
    else:
        req = urllib.request.Request(source, headers={"User-Agent": "CatalogFix-AI/1.9"})
        with urllib.request.urlopen(req, timeout=120) as response:
            data = response.read()
            content_type = response.headers.get_content_type() or ""

    if not data:
        raise ValueError("The supplied catalog file is empty.")

    filename = Path(filename_hint.strip()).name if filename_hint else ""
    if not filename:
        parsed = urllib.parse.urlparse(source)
        filename = Path(urllib.parse.unquote(parsed.path)).name

    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        guessed = mimetypes.guess_extension(content_type) or ""
        if content_type == "application/pdf":
            guessed = ".pdf"
        elif content_type in {"text/csv", "application/csv"}:
            guessed = ".csv"
        elif content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            guessed = ".xlsx"
        elif content_type == "application/vnd.ms-excel":
            guessed = ".xls"
        ext = guessed.lower()

    if ext not in _ALLOWED_EXTENSIONS:
        raise ValueError(
            "Could not determine the catalog type. Set filenameOverride with "
            "a .pdf, .csv, .xlsx or .xls extension."
        )

    if not filename or Path(filename).suffix.lower() not in _ALLOWED_EXTENSIONS:
        filename = f"catalog{ext}"

    return data, filename


def _import_catalog(data: bytes, filename: str):
    name_l = filename.lower()
    pdf_meta = {}
    mapping = {}

    if name_l.endswith(".csv"):
        source_df = pd.read_csv(io.BytesIO(data), dtype=object)
        imported, mapping = standard_import_dataframe(source_df)
        import_report = pd.DataFrame([{
            "sheet": "CSV",
            "source_rows": len(source_df),
            "source_columns": len(source_df.columns),
            "header_products": len(imported),
            "pattern_products": 0,
            "matrix_products": 0,
        }])
        import_mode = "Standard columns"

    elif name_l.endswith(".pdf"):
        with tempfile.TemporaryDirectory(prefix="catalogfix-") as checkpoint_dir:
            imported, import_report, pdf_meta = smart_import_pdf(
                io.BytesIO(data),
                filename=filename,
                chunk_size=100,
                checkpoint_dir=checkpoint_dir,
                resume=False,
                return_meta=True,
                visual_ocr=False,
            )
        mapping = {field: field for field in CANONICAL_FIELDS if field in imported.columns}
        import_mode = "Universal PDF Router"

    else:
        standard_df = pd.read_excel(io.BytesIO(data), dtype=object)
        standard_mapping = map_columns([str(c) for c in standard_df.columns])
        if len(standard_mapping) >= 3 and ("sku" in standard_mapping or "title" in standard_mapping):
            imported, mapping = standard_import_dataframe(standard_df)
            import_report = pd.DataFrame([{
                "sheet": "First sheet",
                "source_rows": len(standard_df),
                "source_columns": len(standard_df.columns),
                "matrix_products": 0,
                "header_products": len(imported),
                "pattern_products": 0,
            }])
            import_mode = "Standard columns"
        else:
            imported, import_report = smart_import_excel(io.BytesIO(data), filename=filename)
            mapping = {field: field for field in CANONICAL_FIELDS if field in imported.columns}
            import_mode = "Smart Import"

    return imported, import_report, pdf_meta, mapping, import_mode


def _records(df: pd.DataFrame) -> list[dict]:
    if df is None or df.empty:
        return []
    return json.loads(df.to_json(orient="records", date_format="iso"))


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        if actor_input.get("automaticTextOnly", True) is not True:
            raise ValueError("This build only supports automatic text-only processing.")
        Actor.log.info("AUTOMATIC_TEXT_ONLY_ENFORCED visual_ocr=false ocr_engine=blocked")
        source = actor_input.get("catalogFile", "")
        filename_hint = actor_input.get("filenameOverride", "")

        Actor.log.info("CatalogFix AI v%s starting", RELEASE_VERSION)
        data, filename = await _download_input(source, filename_hint)
        billing_event, page_count = _catalog_size_tier(data, filename)
        Actor.log.info("Input file: %s (%d bytes)", filename, len(data))
        if page_count is not None:
            Actor.log.info("PDF pages: %d; billing tier: %s", page_count, billing_event)

        imported, import_report, pdf_meta, mapping, import_mode = _import_catalog(data, filename)

        document_type = (pdf_meta or {}).get("document_type", "")
        if imported.empty:
            summary = {
                "catalogfixVersion": RELEASE_VERSION,
                "filename": filename,
                "importMode": import_mode,
                "documentType": document_type or "unknown",
                "productRows": 0,
                "readyRows": 0,
                "reviewRows": 0,
                "issueRows": 0,
                "status": (
                    "skipped-technical-datasheet"
                    if document_type == "technical-datasheet"
                    else "skipped-statistical-report"
                    if document_type == "statistical-report"
                    else "no-product-rows"
                ),
                "pageCount": page_count,
                "billingTier": "catalog-screening" if document_type in {"technical-datasheet", "statistical-report"} else billing_event,
                "message": (
                    "CatalogFix intentionally created 0 product rows because the file "
                    "was classified as a non-catalog document."
                    if document_type in {"technical-datasheet", "statistical-report"}
                    else "No product-like rows were detected."
                ),
            }
            await Actor.set_value("SUMMARY.json", summary, content_type="application/json")
            await Actor.set_value("IMPORT_REPORT.json", _records(import_report), content_type="application/json")
            await Actor.push_data({"recordType": "summary", **summary})
            charge_event = "catalog-screening" if document_type in {"technical-datasheet", "statistical-report"} else billing_event
            await _charge_if_ppe(charge_event)
            await Actor.set_status_message(
                f"Finished: {summary['status']} — 0 product rows",
                is_terminal=True,
            )
            return

        cleaned, issues, shop_ready, needs_review = process_canonical(imported)
        excel_result = to_excel_bytes(cleaned, issues, shop_ready, needs_review, import_report)

        await Actor.set_value(
            "RESULT.xlsx",
            excel_result,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        await Actor.set_value(
            "SHOPIFY_READY.csv",
            shop_ready.to_csv(index=False).encode("utf-8-sig"),
            content_type="text/csv; charset=utf-8",
        )
        await Actor.set_value("IMPORT_REPORT.json", _records(import_report), content_type="application/json")

        quality_stats = (pdf_meta or {}).get("quality_stats", {}) or {}
        summary = {
            "catalogfixVersion": RELEASE_VERSION,
            "filename": filename,
            "importMode": import_mode,
            "documentType": document_type or "catalog-or-unknown",
            "productRows": int(len(cleaned)),
            "readyRows": int(len(shop_ready)),
            "reviewRows": int(len(needs_review)),
            "issueRows": int(len(issues)),
            "mappedFields": int(len(mapping)),
            "qualityStats": quality_stats,
            "pageCount": page_count,
            "billingTier": billing_event,
            "status": "completed",
            "outputs": {
                "workbookKey": "RESULT.xlsx",
                "shopifyReadyCsvKey": "SHOPIFY_READY.csv",
                "importReportKey": "IMPORT_REPORT.json"
            },
        }
        await Actor.set_value("SUMMARY.json", summary, content_type="application/json")

        dataset_rows = []
        for row in _records(cleaned):
            row["recordType"] = "product"
            dataset_rows.append(row)
        if dataset_rows:
            await Actor.push_data(dataset_rows)

        await _charge_if_ppe(billing_event)
        await Actor.set_status_message(
            f"Finished: {len(cleaned)} rows, {len(shop_ready)} Ready, {len(needs_review)} Review",
            is_terminal=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
