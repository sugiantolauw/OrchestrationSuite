# SKILL-001 T&E ExCo — per-test specification

**Status: DRAFT — pending audit approval.** Required by CLAUDE.md §0.2 and the P2 definition of
done: written *before* porting, and it wins wherever `computation.py` disagrees. Each item marked
**[PENDING]** is a judgement made by the build that an auditor must confirm or change; the build
proceeds on the stated default and the UI labels it.

Data profiled 2026-09-23 from `synthetic_data/` (row counts match `FILE_REGISTRY` exactly).

---

## 0. Rules common to every test

| Rule | Specification |
|---|---|
| **ExCo identity** | By `Employee ID`, never by name (§0.5). IDs: 52725, 52394, 52622, 52424, 51867, 52660, 51606, **52472**, 52674, 52703, 50079. **[PENDING]** "Delacroix, Marie" also has ID 50040 (27 claims) — excluded by default because 52472 is the ID that appears as an approver in both expense and approval data, so 52472 is taken to be the executive and 50040 a namesake. |
| **Name-only sources** | `travel_requests_no_expense`, `travel_request_segment` and `booking_detail` carry no employee ID. They are matched on the exact trimmed display name of the ExCo IDs above. Every name join reports its match rate. This is a declared exception to the ID rule, forced by the source. |
| **Expense population `P_EXP`** | Rows of `expense_report` where `Employee ID` ∈ ExCo **or** `Employee ID(Cross Change Approver)` ∈ ExCo, **as a set union** (a row counts once). Each row carries `role ∈ {prepared, approved, both}`. `computation.py` concatenated the two sets, which counts rows in both twice (23 rows in this data). With the default ExCo IDs (50040 excluded): 3,711 rows before the period filter (prepared 1,417 / approved 2,317 / both 23), 3,628 in the period. Including 50040 would add 27 prepared rows (3,738 / 3,654). |
| **Audit period** | 2025-01-01 to 2026-04-30 inclusive, calendar dates in **Australia/Sydney**. The source dates carry no time component, so they are compared as dates. Rows outside the period are excluded and counted (`out_of_period_rows`). The source contains dates back to 2017. |
| **Currency** | `Reimbursement Currency` must be `AUD` on every row of every source that carries it; any other value fails the run (G7). Amounts come from `Expense Amount (reimbursement currency)`. Transaction currency is used only to derive country (T6.1d). |
| **SGD per-diem conversion** | A$ = S$ × (1 / RBA F11.1 monthly mean of `A$1=SGD` for the transaction's month), from the pinned snapshot `synthetic_data/reference/RBA_F11.1_exchange_rates_published_2026-09-22.csv`, hashed into the run fingerprint. |
| **Whitespace** | Headers **and** categorical values are trimmed (`Advance Purchase Days `, `Staff Amenities `, `"No "`). A declared contract rule — no other normalisation, no case-folding, no substring matching of column names. |
| **Row identity** | Each source row's key is `(source_sha256, row_number)` — 1-based data-row number in the pinned file. Every flagged row cites this key (NN10). |
| **Negative and zero amounts** | 37 negative and 3 zero rows in `P_EXP` (credits and reversals). They stay in the population and in reconciliation (G6). They are excluded from exceedance, split and duplicate tests (a credit cannot exceed a limit or split a claim). Each such test reports the excluded count. |
| **Exact duplicate source rows** | Rows identical in every column (87 in the full file, 1 in `P_EXP`) are reported as a data-quality metric. They are **not** removed from the population. T5.2 treats them as duplicate candidates like any other row. |
| **Missing inputs** | A missing required column or source fails the run. There is no zero result for "could not compute" — that is `not_testable` with a reason. |

---

## 1. The tests

Each entry gives: population · grain · join key · exception identity · **scoring unit** ·
threshold · exclusions · expected evidence · primitive · deviation from `computation.py`.

### T3.1a — Pre-approvals not linked to expense reports
- **Population:** `travel_requests_no_expense` rows whose `Employee` is an ExCo name, `Start Date` in period, and `Approval Status` ∈ {`Approved`, `Approved - Pending Booking`}.
- **Grain / identity / scoring unit:** a travel request (`Travel Request ID`).
- **Exception:** every request in the population. The source is itself the system's "authorised request without expense entry" register.
- **Threshold:** 0 (catalogue).
- **Exclusions:** `Cancelled` and `Sent Back to Employee`. **[PENDING]** Are cancelled requests out of scope?
- **Evidence:** request ID, employee, start date, policy, `Total Approved Amount (rpt)`.
- **Metrics:** `preapproval_unlinked_count`, `preapproval_unlinked_amount`, `preapproval_employees`.
- **Primitive:** `list_membership` (column `Approval Status`, allowed list).
- **Deviation:** `computation.py` used fuzzy amount-column selection and applied no status filter.
- **Limitation:** relies on a system-generated register, which cannot be independently re-derived because the expense report has no travel request ID.

### T3.1b — Travel booked without pre-approval
- **Population:** `booking_detail` rows whose `Lead Traveller Name` is an ExCo name, `Depart Date` in period, and `Booking Status` ≠ `Cancelled`.
- **Join key (user decision):** employee name + travel class.
  - Every `Booking Type` value is `Business Travel`, so the class comes from `Dom | Int`: `Domestic` ↔ request `Expense Type` ending `- Domestic Travel`; `International` and `Trans Tasman` ↔ `- International Travel`. **[PENDING]** Confirm this reading of "employee name + expense type".
  - **[PENDING]** Add a date condition (request `Start Date` within ±3 days of `Depart Date`)? Without one, any request of the same class at any time satisfies the join, which hides unapproved trips. Default: no date condition, as instructed.
- **Grain / scoring unit:** a booking (`Booking ID`).
- **Exception:** a booking with no matching request.
- **Evidence:** booking ID, traveller, depart date, class, match rate.
- **Metrics:** `no_preapproval_bookings`, `no_preapproval_employees`, `t31b_name_match_rate`.
- **Primitive:** `anti_join_gap`.
- **Deviation:** `computation.py` joined on name only, with no class, and its catalogue metric counted employees rather than bookings. Here both are reported and the scoring unit is the booking.

### T3.2a — Non-preferred supplier usage
- **Source correction:** `booking_detail` has **no supplier column**. `computation.py` read `Supplier Name`, which does not exist. The test runs on `P_EXP` using `Vendor`.
- **Population:** `P_EXP` rows with `Expense Type` ∈ {Airfares, Accommodation, Car Rentals} × {Domestic, International} Travel. Excludes vendor `FCM` (the travel agency: its lines are agency charges, not the supplier).
- **Preferred lists [PENDING — analyst-set, no policy source]:**
  - Airlines: Qantas, QantasLink, Jetstar, Virgin Australia — matched as whole words in the upper-cased vendor (e.g. `QANTAS AIRWAYS (I)`, `VIRGIN AUSTRALIA - AU - AGENCY`).
  - Car: Avis, Budget, Hertz.
  - Hotels: **none supplied**. The accommodation sub-tests are `not_testable`, reason "no preferred-hotel list supplied".
- **Grain / scoring unit:** an expense line.
- **Metrics:** `pref_dom_airline`, `pref_int_airline`, `pref_dom_car`, `pref_int_car` (counts of non-preferred lines); `pref_dom_accom` and `pref_int_accom` are `not_testable`.
- **Primitive:** `list_membership` (negate).

### T3.3a — Late / urgent travel bookings
- **Population:** as T3.1b (ExCo bookings in period, not cancelled).
- **Threshold [PENDING — analyst-set]:**
  - late = `Advance Purchase Days` < 7 (Domestic) or < 14 (International, Trans Tasman);
  - very late = < 3 (any class).
  - `computation.py` declared these constants but then applied 14 to every booking ("simplified"), and the catalogue said "< 14". The class split is used here, and the catalogue is updated to match (G8).
- **Grain / scoring unit:** a booking.
- **Metrics:** `late_booking_count`, `very_late_booking_count`, `late_booking_employees`.
- **Primitive:** `threshold_exceedance` (direction below), one instance per class.
- **Deviation:** `computation.py` fuzzy-matched the column and returned 0 when it was missing. A missing column now fails the run.

### T3.3b — Entertainment per-head threshold
- **Source:** `attendee_validity`. Each row is one attendee of one entertainment entry.
- **Entry identity:** `(Employee ID, Report Name, Transaction Date, Vendor, Entry Amount)`. The source has no entry key.
- **Population:** entries of ExCo claimants (`Employee ID`) in period, with `Expense Type` ∈ {`Staff/Client Function: Offsite Food/Drink`, `Staff/Client Function: Onsite Food/Drink`}.
- **Per-head spend:** `Entry Amount` / `Number of Attendees` (fixes defect §0.2 `:202`, which compared the whole claim).
- **Threshold [PENDING — analyst-set]:** $40 per head if every attendee is internal, $80 if **any** attendee is external (P2 build-brief correction, B5: the first port aggregated the per-row internal/external classification with the FIRST attendee row read for the entry, not `any` — `ratio_per_group`'s `limit_selector_aggregate: any` parameter now implements the "any attendee" rule literally). External = `Company` or `External ID` present. **[PENDING]** Confirm the internal/external rule. On this data the rule classifies **every** entry as external: `Company` is null on all 1,394 attendee rows and `External ID` is populated on all of them, so it is probably populated for employees too. A usable rule needs to know what `External ID` holds. **Consequence for Surface 2:** because `External ID` is non-null on every real row, `any` and `first` never disagree on this data, so the `any` fix cannot be exercised through `tests/fixtures/tne_planted/`; it is covered by a direct primitive-level test instead (`tests/test_p2a_primitives.py::test_ratio_per_group_limit_selector_aggregate_any_vs_first`) against a synthetic population not bound by this contract.
- **Grain / scoring unit:** an entertainment entry.
- **Metrics:** `ent_assessed`, `ent_over_internal_count`, `ent_over_external_count`, `ent_over_amount`.
- **Primitive:** `ratio_per_group`.

### T4.1 — Missing receipt documentation
- **Population:** `P_EXP`.
- **Exception:** a `P_EXP` line present in the `missing_receipt` register. Semi-join on `(Employee ID, Report Legacy Key, Transaction Date, Expense Type, Expense Amount (reimbursement currency))`. Register rows that fail to join are reported (`t41_unmatched_register_rows`).
- **Grain / scoring unit:** an expense line.
- **Metrics:** `missing_receipt_count`, `missing_receipt_pct` (of `P_EXP` lines), `missing_receipt_amount`, `missing_receipt_no_affidavit_count` (`Has Affidavit` ≠ `Yes`).
- **Primitive:** `anti_join_gap` (mode `semi`).
- **Deviation:** `computation.py` defaulted `all_have_affidavit = True` ("default assumption based on audit results"). Here it is computed.

### T4.2 — Missing attendee details
- **Population:** `P_EXP` lines with `Expense Type` ∈ {`Staff/Client Function: Offsite Food/Drink`, `Staff/Client Function: Onsite Food/Drink`, `Staff/Client Function: No Food/No Drink`}. **[PENDING]** The catalogue also names gifts; gifts have no attendees, so they are excluded by default.
- **Exception:** no attendee record for the line. Anti-join to `attendee_validity` on `(Employee ID, Transaction Date, Expense Type, amount)`, where `Entry Amount` = `Expense Amount (reimbursement currency)` to the cent.
- **Grain / scoring unit:** an expense line.
- **Metrics:** `att_total`, `att_missing_count`, `att_missing_pct`.
- **Primitive:** `anti_join_gap`.
- **Deviation:** `computation.py` fuzzy-picked columns and, lacking an attendee column, marked every row missing.

### T4.3 — Potential personal expenses
- **P2 result:** `not_testable`, reason "classification endpoint not yet wired (P6 `classify` node)". The cached `flagged: 132` is deleted, not ported.
- **P6:** GPT-OSS via `ai_query` over `P_EXP` descriptions, with a confidence score. Rows coded `Personal Expense-Corp Card Only (Non-Reimbursable)` (87) are correctly declared personal and are **not** exceptions.

### T4.4 — High-value reimbursements
- **Population:** `P_EXP`, amount > 0 (N5, P2 build-brief correction: the first port ran this test against unfiltered `P_EXP`; `p_exp_hv` now applies the amount > 0 filter §0 already specified, with the excluded count reported at `p_exp_hv.excluded.*`).
- **Exception:** amount > $5,000 (strict; catalogue).
- **Grain / scoring unit:** an expense line.
- **Metrics:** `hv_count`, `hv_amount`, `hv_employees`.
- **Primitive:** `threshold_exceedance`.
- **Deviation:** `computation.py` mutated its input frame in place (`pd.to_numeric` on the shared population).

### T5.1 — Potential split claims
- **Population:** `P_EXP`, amount > 0, vendor not in {`FCM`, `THE MOVING COMPANY`} (exact trimmed match, not substring).
- **Same-day group:** `(Employee ID, Vendor, Transaction Date, Expense Type)` with count > 1 **and** sum > $5,000 **and** every line ≤ $5,000. **[PENDING]** The last condition is new: a split is by definition made of parts that are each under the limit. Without it, a single $6,000 line plus a $10 line counts as a "split".
- **Window group:** within `(Employee ID, Vendor, Expense Type)`, every 3-calendar-day window `[d, d+2]` anchored on each line's date. A window is an exception when it has more than one line, its **lines in the window** sum to > $5,000, and each line is ≤ $5,000. Overlapping windows are merged into one claim group.
- **Defect fixed:** `computation.py:365` summed the whole employee/vendor/type group once any pair fell within 2 days. The bug at `:390` (`dir()`) disappears with the rewrite.
- **Claim identity (P2 build-brief correction, B3):** a claim group's identity is its ROW MEMBERSHIP, not a key tuple. Same-day detection and window detection run independently and can each find a group; if a window detection's member rows are the IDENTICAL set to a same-day detection's, they are the same claim and are counted once. Same-day and window detections with DIFFERENT member sets (e.g. a same-day pair that is also, correctly, part of a larger merged window) remain distinct claims. The first port scored every same-day and window detection as its own unit keyed on `(group_keys [+ date])`, which double-counts every case where a same-day pair is also independently found by the window pass over the identical rows — this is the population-inflation defect CLAUDE.md §0.2 describes, reproduced by scoring on key tuples instead of row membership.
- **Grain:** line flags plus claim groups.
- **Scoring unit:** the claim group (distinct, deduplicated by member row set).
- **Metrics:** `split_same_day_groups`, `split_window_groups` (kind-specific: how many detections each method made, NOT deduplicated against each other), `split_groups` (the distinct claim-group count findings.yaml cites), `split_lines` (distinct flagged lines), `split_amount` (over the distinct lines).
- **Primitive:** `split_detection`.

### T5.2 — Duplicate expense claims
- **Population:** `P_EXP`, amount > 0.
- **Duplicate key:** `(Employee ID, Transaction Date, Vendor, amount)` exact. Lines sharing a `Parent Key` are excluded as itemisations of one entry, not duplicates. **[PENDING]** Confirm itemisation semantics.
- **OOP vs card sub-test:** a duplicate group containing both `Out of Pocket` and a corporate card (`Amex IBCP`, `ANZ Visa CBCP`).
- **Grain:** lines.
- **Scoring unit:** the duplicate group.
- **Metrics:** `duplicate_groups`, `duplicate_lines`, `duplicate_amount` (sum of the extra lines beyond the first in each group — the amount at risk), `duplicate_oop_card_groups`.
- **Primitive:** `duplicate_detection`.
- **Deviation:** `computation.py` keyed on employee **name**, picked any column containing "payment"/"card"/"method", and counted rows rather than groups.

### T6.1a — Approver review sufficiency (`custom.py`)
- **Population:** `approval_aging` rows with `Approver ID` ∈ ExCo and `Approved Date/Time` in period.
- **Grain / scoring unit:** an approval step `(Report ID, Step, Approver ID)`. A report can have up to 3 steps.
- **Receipt viewed:** `Report Receipt Viewed` = `Yes` OR `All Entry Receipts Viewed` = `Yes` (the actual column names; `computation.py` referenced non-existent ones and silently set every row to "not viewed").
- **Instant:** viewed (the two-column rule above, **not** `Receipts Viewed Date` — a P2 build-brief correction: `Receipts Viewed Date` is evidence of when a receipt was viewed, not a substitute definition of "was it viewed") AND `Minutes of Approval from Receipt View` < `thresholds.instant_approval_minutes` (1). **[PENDING]** For steps where no receipt was viewed, `Minutes of Approval from Receipt View` is undefined; instant is assessed only on steps where receipts were viewed (the two-column rule).
- **Exception:** not viewed, OR (viewed AND instant). Not viewed is unconditionally an exception — it does not also require worst-case timing. Worst case is a separate, always-defined comparison: not viewed AND approved within the instant threshold of `Approver Received Date` (never the exception definition; reported as `approver_worst_case_n`/`_pct` only).
- **Metrics:** `approver_total_reports`, `approver_count`, `approver_no_receipt_pct`, `approver_instant_pct`, `approver_worst_case_n`, `approver_worst_case_pct`, plus a per-approver table.
- **P2 build-brief correction:** the first port of this test used `Receipts Viewed Date` (a date/notna check) to gate "instant", and used `worst_case` (not viewed AND approved within the instant threshold of `Approver Received Date`) as the exception definition itself — which missed every not-viewed step that wasn't ALSO approved within the instant threshold, and missed every viewed-and-instant step entirely (a step can only be `worst_case` when NOT viewed, by construction, so "viewed and instant" was never flagged at all). Both are fixed above; the oracle in `tests/fixtures/tne_planted/plants.yaml` was rebuilt from this corrected text, not from either version of the code.

### T6.1c — Attendee hierarchy validity
- **Population:** `attendee_validity` rows of ExCo claimants, in period.
- **Exception:** `Is Attendee also in Hierarchy?` = `Yes` (after trimming; the source has `"No "`).
- **Grain / scoring unit:** an attendee row (entry × attendee).
- **Metrics:** `att_hierarchy_total`, `att_hierarchy_invalid`.
- **Primitive:** `list_membership`.
- **Deviation:** `computation.py` returned a hardcoded `0` ("based on audit findings"). It is now computed.

### T6.1d — Daily spend over per-diem
- **Population [PENDING]:** claims **prepared by** ExCo (`Employee ID` ∈ ExCo), not the approved set. Daily spend is the traveller's own. `computation.py` grouped the combined population, which mixes in other people's claims that an ExCo member merely approved. Expense types in scope: Meals and Incidentals, Domestic and International Travel. A per-diem covers meals and incidentals, not airfares or accommodation. **[PENDING]** Confirm the scope.
- **Country (user decision: city + transaction currency):**
  1. `City/Location` present → declared city→country lookup (`reference/city_country.yaml`, analyst-set).
  2. City absent → `Transaction Currency` when the currency belongs to one country (AUD→Australia, SGD→Singapore, NZD→New Zealand, PHP→Philippines, THB→Thailand, INR→India, KRW→Korea, South, MYR→Malaysia, FJD→Fiji, CHF→Switzerland).
  3. Otherwise (USD, EUR) → **unmapped**: excluded and counted (`t61d_unmapped_rows`), never defaulted.
- **Grain / scoring unit:** employee-day-country `(Employee ID, Transaction Date, country)` (fixes defect `:562`).
- **Negative and zero amounts (N5, P2 build-brief correction):** excluded from the population before the daily sum, per §0 -- a credit is not a spend event and must not net against same-day positive lines. The first port summed the raw column, which let a same-day credit hide a genuine exceedance. Excluded count reported at `t61d_pop*.excluded.*`.
- **Limit:** `Rate Per Day (S$)` for the country × monthly RBA A$/S$ factor. A country missing from the per-diem table → the day is `not_testable`, reported.
- **Exception:** daily total > limit.
- **Metrics:** `daily_over_count` (employee-days), `daily_over_amount` (excess over limit), `daily_over_employees`, `daily_over_max`, split `_domestic` (Australia) / `_international`.
- **Deviation:** the `$1,000` workaround (`app.py:415`), the `500` fallback (`:576`) and the hardcoded `intl_exceptions: 2` (`:590`) are removed. The catalogue is updated to the per-diem rule (G8).

---

## 2. Run-level metrics (all computed)

`total_records` = Σ rows across loaded sources; `total_files` = number of sources loaded;
`months_covered` = distinct year-months of the period; `claims_prepared`, `claims_approved`,
`claims_combined` = rows and amounts of the three role sets (combined is the union). These replace
the constants `152_921`, `8` and `16` in `computation.py:641–643`.

## 3. Scoring units for Surface 2 (planted data)

Precision and recall are computed per test on the scoring unit above, against plants declared
in a sidecar file (§9) — never inferred from the generator.

| Test | Scoring unit |
|---|---|
| T3.1a | travel request |
| T3.1b, T3.3a | booking |
| T3.2a, T4.1, T4.2, T4.4 | expense line |
| T3.3b | entertainment entry |
| T5.1, T5.2 | claim group |
| T6.1a | approval step |
| T6.1c | attendee row |
| T6.1d | employee-day-country |
| T4.3 | not scored until P6 |

## 4. Open items for the auditor

Every **[PENDING]** above, plus:

- Policy sources for every threshold. All are currently `provenance: analyst-set`, `pending_policy_confirmation: true`.
- Preferred-supplier lists (airline, car, hotel).

## 5. Surface 2 gate hardening (P2 build-brief, independent review)

An independent review found that Surface 2's scoring compared the engine's flagged units only to
the natural ids a test's own `plants.yaml` block declared, so an engine flag on a row/group the
fixture never mentioned for that test was neither a true nor a false positive -- invisible, not
scored. Several realistic engine bugs (T3.1b joining on name only; T5.1 summing the whole
employee/vendor/type group instead of the matching window; T5.1 same-day detection disabled;
T6.1d aggregating a day with `max()` instead of `sum()`; the ExCo membership filter dropped from
every population) passed Surface 2 unchanged as a result. `tests/fixtures/tne_planted/plants.yaml`
now declares targeted plants and negatives for each of these (ExCo background rows the mutation
would over-flag; non-ExCo rows that would become exceptions if a population filter broke;
same-day/window claims whose row membership discriminates a correct detection from a buggy one),
and `tests/test_surface2_mutations.py` applies each mutation in-process and asserts Surface 2
fails for the affected test -- a permanent regression gate on the scoring gate itself. Not
addressed: an exhaustive audit of every legitimate cross-test overlap across all 21 tests (several
tests share one source population, so a row planted for one test can correctly and legitimately
also satisfy another's exception condition -- see `orchestrator/eval/surface2.py`'s module
docstring), which would be required before Surface 2 could compare against a test's FULL flagged
population rather than its own declared scope.
