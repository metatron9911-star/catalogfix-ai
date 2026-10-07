# CatalogFix AI — Product Catalog Cleaning & Data Quality Audit

**Turn messy supplier catalogs into structured product data with a clear Ready / Needs Review split.**

CatalogFix AI cleans supplier PDF, Excel, and CSV catalogs before ecommerce or marketplace import. It normalizes product fields, flags missing or invalid commercial data, finds duplicates, preserves source context, and keeps uncertain records out of the Ready set instead of inventing values.

## What you get

Every successful catalog run can produce:

- **RESULT.xlsx** — workbook with Clean Master, Issues Found, Shopify Ready, Needs Review, and Import Report
- **SHOPIFY_READY.csv** — only rows that pass the current quality gates
- **SUMMARY.json** — document type, row counts, and quality statistics
- **IMPORT_REPORT.json** — page/sheet routing and parsing audit
- **Dataset** — normalized product records with source and quality context

## Popular use cases

### Clean a supplier Excel catalog
Normalize column names and product fields, identify missing SKU/price data, and separate records that are safe to import from those that need attention.

### Convert a supplier PDF catalog to product data
Extract product rows from text-based or image-only PDFs, including OCR-heavy catalogs.

### Find duplicate products before import
Surface duplicate or conflicting catalog rows before they reach Shopify, a marketplace, PIM, or feed.

### Check missing SKU and prices
Flag products where supplier SKU, price, or other required commercial fields are absent or uncertain.

### Clean ecommerce CSV before import
Use CatalogFix as a QA gate before uploading a CSV to your storefront or product feed.

### Audit product data before marketplace upload
Review catalog quality before a marketplace import and separate clean records from review-only records.

## Ready vs Needs Review

CatalogFix does not treat every extracted row as equally trustworthy.

### Ready

A row is placed in **Ready** only when it passes the current release gates for required product data and quality.

### Needs Review

A row is placed in **Needs Review** when something important is missing or uncertain, for example:

- supplier SKU is missing or not confidently verified
- price or required commercial data is missing
- OCR confidence is low
- title or product-card boundaries are uncertain
- duplicate or conflicting records need inspection

A run can legitimately return **zero Ready rows** while still extracting useful candidates. That is intentional behavior, not a failure.

## Safety behavior

**CatalogFix does not invent missing supplier data.**

- Missing supplier SKUs are not fabricated.
- Internal candidate IDs remain review-only.
- Technical datasheets can be rejected with zero product rows.
- Visual OCR keeps source location and confidence metadata.
- Structured price sources preserve provenance where available.
- Quality gates keep uncertain records out of the Ready export.

## Input

Upload one supplier catalog per run.

Supported formats:

- PDF
- CSV
- XLSX
- XLS

You can also provide a direct HTTP(S) URL.

The Apify Store input is prefilled with a tiny synthetic demo catalog so first-time runs and Apify automated quality checks finish quickly. Replace it with your own file for production use.

## How it works

1. Upload a catalog.
2. CatalogFix classifies the document and routes pages or sheets by content type.
3. Structured parsers handle commercial tables and order forms where possible.
4. Image-only pages use OCR.
5. Quality checks normalize and validate commercial fields.
6. Records are separated into Ready and Needs Review.
7. Download the structured data and audit files.

## Pricing

CatalogFix uses pay-per-event pricing with one catalog charge per completed run:

- **Up to 50 pages:** $4.90
- **51–200 pages:** $12.90
- **201–500 pages:** $24.90
- **Technical/statistical non-catalog screening:** $1.00

The current self-service Store version accepts PDFs up to **500 pages**.

## 60-second demo flow

**Upload supplier CSV/PDF → normalize product fields → flag missing/invalid/duplicate data → split Ready from Needs Review → download structured output and audit files.**

The public Example Tasks in the Apify Store provide one-click scenarios for common catalog-cleaning jobs.

## API, automations, and AI agents

CatalogFix can be run from the Apify Console, API, scheduled Tasks, integrations, and Apify MCP workflows. Public Example Tasks cover common catalog-cleaning jobs so users and AI agents can start from a concrete workflow instead of building an input from scratch.

Common automation pattern: **supplier file → catalog QA → Ready / Needs Review → structured export or downstream ecommerce workflow**.

## Notes and limitations


- OCR-heavy PDFs take longer than text-based catalogs.
- A second OCR pass may be triggered when the first pass is not reliable enough.
- A real supplier SKU can still remain in Needs Review if other required fields are missing.
- CatalogFix does not promise perfect extraction; it makes uncertainty explicit and auditable.

## Current release

**CatalogFix AI v1.9.0**
