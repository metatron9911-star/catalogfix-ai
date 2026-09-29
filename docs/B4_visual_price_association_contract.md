# B4 Visual SKU ↔ Price Association — accepted contract

Status: **CLOSED / PASS**

Accepted implementation commit: `ec252919c8bd267e4cccc1a587390bfb6ea9cf90`  
Acceptance build: **Apify 0.0.92**  
Frozen pre-B4 baseline: `9e274411efbd9a0925e4c9c4a3ca687cd20dc497`

## price_source contract

`attributes_json.price_source` has five defined values for the visual price path:

| value | meaning |
|---|---|
| `missing` | Legacy / pre-B4 default state. It does **not** mean that B4 ran and failed. |
| `visual-associated` | B4 ran and exactly one owned eligible price candidate was associated with the supplier SKU. |
| `visual-ambiguous` | B4 ran and two or more owned eligible price candidates were found; price fails closed to null. |
| `visual-association-skipped` | B4 was applicable but safe association could not run, including physical SKU collision, same-lane/same-cy ambiguity, or unusable/missing selected-pass anchor geometry. Price fails closed to null. |
| `visual-associated-none` | B4 ran successfully but found zero owned eligible price candidates. Price remains null. |

**Important:** `missing` ≠ “B4 could not associate a price.” B4 outcomes are represented by the four `visual-*` states above.

## Dedupe integration contract

The following B4-emitted null states are protected through `_dedupe_imported`:

- `visual-ambiguous`
- `visual-association-skipped`
- `visual-associated-none`

For those base records, only the `price` field is protected from duplicate-SKU enrichment. Other fields retain the existing merge behavior. Legacy `missing` remains enrichment-compatible.

## Migration note for downstream consumers

Any downstream consumer that currently interprets `price_source="missing"` as a B4 association outcome must be updated to distinguish legacy/default `missing` from `visual-associated-none`. Analytics, ops/review logic, and exports should treat the explicit `visual-*` states as the B4 contract.

## Acceptance record

Commit 6 (`47041ce0d4f9ad7d94ef1349b55be6b7575afb6d`) was the first production-visible B4 wiring. Commit 7 (`ec252919c8bd267e4cccc1a587390bfb6ea9cf90`) added the B4 → dedupe protected-null integration contract.

Accepted run characteristics on build 0.0.92:

- 144 total rows;
- 54 `visual-high-intelligence` rows;
- 28 `visual-associated`;
- 17 `visual-associated-none`;
- 7 `visual-association-skipped`;
- 2 `visual-ambiguous`;
- no changes to `quality_confidence`, `quality_flags`, or `visual_confidence` versus the accepted B4 wiring dataset;
- 9 `CAND-*` records remained field-equivalent to frozen 0.0.73 behavior.

Corrected prereg examples:

- BRAVO60 → `visual-association-skipped`, price null;
- BRAVO78 → `visual-associated`, price 27990, source page 12;
- BIO-01 → `visual-associated`, price 55690;
- BIO-02 → `visual-associated`, price 49490;
- BIO-05 / BIO-06 / IN-590 → `visual-associated-none`, price null;
- UPTO1250 → `visual-ambiguous`, price null;
- PRICE60 → `visual-ambiguous`, price null.

### Predicted downstream effect

PRICE60 changed from `qa_status=READY` to `qa_status=NEEDS REVIEW` when the invalid enriched price was restored to protected null. This was the preregistered consequence of the missing-price Critical and is **not a regression**.

## Explicit residuals outside B4

These are separate tasks and do not reopen B4 geometry:

1. **B3-B — visual code candidate precision.** Six of the 28 associated records in the acceptance run are known false supplier-code candidates: `UPTO1200`, `UPTO1350`, `PRICE75`, `UPTO1700`, `UPTO1800`, `FAN-30`.
2. **Multi-page SKU policy.** Generic dedupe currently has no explicit policy for a canonical SKU that receives different valid page-local associated prices on multiple pages.
3. **Downstream rollout decision.** Make this decision after B3-B so rollout is evaluated against the post-precision associated set rather than the provisional 28-row set.

B4 commits 1–7 are preserved as history; commits 2–5 were production-neutral, commit 6 introduced the first intended production diff, and commit 7 repaired the downstream protected-null invariant.
