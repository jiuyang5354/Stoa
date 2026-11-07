# Financial evidence and promotion contracts

## SEC evidence

`Sources.add("sec_xbrl", ...)` keeps companyfacts and frames facts separate from
unstructured numeric evidence. `Sources.sec_match()` selects facts using company,
concept, economic period, unit and any explicit citation before checking the value.
It does not select a restatement because its number happens to match the answer.
Conflicting values for the same selected identity are unverified; equivalent
repeated disclosures are allowed.

This adapter covers the actual `edgar.run_tool("sec_xbrl", ...)` shape. It does not
change `edgar.py` or the tool payload. Frame rows retain their own company identity
rather than inheriting a requested symbol. SEC values do not enter the generic
numeric pool or the second-pass numeric reuse path, which would otherwise allow
semantic binding to be bypassed.

The SEC and existing `get_financials` checkers share current-clause period and currency
constraints. When SEC lacks a metric, fallback is only to matching structured financial
facts, never to an arbitrary number in another source. An explicit financial citation
must match that particular source's company, metric, period and currency. Rejected
financial citation markers are removed during final rendering; invalid pointers are
checked before removal so they cannot silently become automatic source selection.
Unknown explicit currency codes remain constraints. Exact date intervals are not
verified by financial rows that lack original interval boundaries.

Coverage is intentionally bounded: revenue, net income, total assets, exact XBRL
tags, recognized currency expressions and explicit economic periods. Calendar
quarters are distinct from year-to-date intervals; an absent `start` is not an
annual duration, and a filing's `fy`/`fp` alone is not an economic period. Financial
quarters with unknown boundaries are not guessed as full years. This is not a
universal natural-language financial verifier. Conflicting-disclosure rejection applies
to SEC raw facts, not every financial adapter: the existing `get_financials` matcher
still checks the answer against candidate rows and can accept either value when
otherwise matching rows disagree. This pre-existing policy is unchanged here.

`tests/test_cite.py` pairs same-number wrong-company/metric/period/currency cases
with valid values, unit scaling, rounding, equivalent disclosures, instant facts,
and the real offline EDGAR adapter shape. The checker is exercised through
`verify`, `flag` and `finalize`, not only an isolated helper.

## Offline evolution and model transfer

`gate()` now rejects any per-case holdout regression, even when the holdout average
improves. Known safety categories (`风控`, `对抗`, `引用`, `拒绝编造`) are also protected
when split into training. Both offline evolution and model-transfer callers pass
these category-derived IDs; unknown external categories still receive the
conservative per-holdout protection.

Required IDs must be present in both baseline and candidate, with finite numeric
scores. Duplicate IDs and overlapping train/holdout IDs are rejected. Task splitting
rejects duplicate IDs before evaluation can overwrite dictionary entries. An
incomplete baseline stops proposing changes and produces an unverified report.

Training improvement, prompt-growth limits, approvals, rollback, explicit user
corrections and model-specific applicability retain their existing contracts.
Ordinary training scores may still trade off against each other. The safety rule
is per-case scalar non-regression, not a veto over each individual check within a
case. Equal scores are allowed for model transfer, not ordinary improvement.

## Deferred work

Online review admission is unchanged. This patch does not add optimizer-exposure
lineage, a hidden test set, verifier process isolation, policy fingerprints, raw-result
pagination, or remote notification reconciliation. Its tests use offline fixtures;
they do not establish a production model-quality or cost improvement.
