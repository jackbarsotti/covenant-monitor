# Error analysis

One row per eval failure: what failed, why (category), the fix, and the score after
re-running. Categories: extraction, review, ratio inputs, scoring, labels, Q&A, memo.

| Run | Company | Failed check | Category | Cause | Fix | Re-run result |
|---|---|---|---|---|---|---|
| 1 | all | covenant found / threshold / schedule / frequency / ratio (13 checks) | extraction | Body headings run into their titles after HTML stripping ("9.1.Financial Covenant."), so only table-of-contents lines matched. The last TOC entry swallowed the whole agreement (BSX "13.22": 388K chars); covenant sections were empty TOC stubs and no covenants were extracted. | Heading pattern accepts no space after the number and no end-of-line anchor. | Run 2: found 4/4, frequency 4/4, ratio 4/4. Fixed. |
| 1 | BSX | addbacks, quotes_verbatim | extraction | Same root cause: nothing extracted. | Same fix. | Run 2: both pass. Fixed. |
| 1 | all | question[0–2] (6 of 9 failed) | Q&A | Definitions were filed under the last TOC section ("11.06 “Consolidated EBITDA”"), and quotes cited to a parent section ("1.1") weren't found in its split-out definitions. | Splitter fix; quote checks search a section and its split-out definitions. | Run 2: questions 15/15. Fixed. |
| 1 | — | judge agreement 80% (not trusted) | memo / judge | The memo is told to say EBITDA excludes add-backs, but the facts table didn't contain that fact, so the judge called it unsupported in 2 faithful calibration memos. | Add an `ebitda_basis` fact to memo facts and the calibration set. | Run 2: agreement 100%, trusted; memos 3/3. Fixed. |
| 1 | CACI | (pre-empted) covenant ratio | ratio inputs | Found while labeling, not by run 1 (extraction failed first): CACI tags a depreciation-only figure as DepreciationDepletionAndAmortization ($77.6M); total D&A is $245.9M. | Use the largest D&A candidate for the year. | Run 2: CACI ratios 2/2 within 0.01x. Fixed. |
| 2 | BSX | threshold_in_effect, schedule | extraction | The "Maximum Leverage Ratio Step-Ups" definition reached extraction, but the agent kept only the 3.75x base. The prompt didn't say how to treat step-ups the agreement deems in effect for a past acquisition. | Prompt: convert deemed step-ups to dated levels; leave out elective ones (matches labeling policy 012). | Run 3: threshold in effect 4.50x, passes. Schedule still failed on representation; see run 3 row. |
| 2 | CACI | schedule (2 covenants) | extraction | The opening level was dated with the first test date (2025-12-31) instead of null. | Code: sort each schedule and set the earliest level's effective_from to null. | Run 3: CACI 19/19. Fixed. |
| 2 | STX | addbacks (precision 1.00, recall 0.33) | extraction | The prompt asked only for the EBITDA definition's add-backs; the labels also count Consolidated Net Income exclusions that raise EBITDA (policy 012). | Prompt: one entry per clause, including net income exclusions named for their charge side; no income inclusions; conditions in the name. | Run 3: recall 0.33 → 0.83; precision 0.69, still failing; see run 3 row. |
| 3 | BSX | schedule | scoring | The agent encoded the schedule as a 3.75x base "from start" plus dated step-ups from 2024-12-31; the label starts at 4.75x. Both give the same limit on every date from 2024-12-31 on, but the check compared exact step lists. | Compare the limit in effect at every date either schedule changes. | Rescored run 3: passes. |
| 3 | STX | addbacks (precision 0.69) | scoring | The agent split one clause (contract termination, facility closure, startup, unused capacity) into four items, which one-to-one matching counted as extras. The stemmer also mismatched "losses"/"loss" and "facilities"/"facility". | Set matching (an item counts if it matches any label and vice versa), a real plural stemmer, and generic words ("costs", "charges") ignored so they can't pair unrelated items. | Rescored run 3: precision 1.00, recall 0.92 (still misses accounting-change and inventory charges). Passes. Score 100% (47/47). |

## Current state (baseline)

- Score 100% (47/47) on run 3, rescored with the final scoring code. Cost $3.39 per full run.
- Known gaps still visible in details: STX misses 2 of 24 add-backs. Three companies is a small set; the next labels should target cases the agent hasn't seen (covenant holidays, springing tests, EBITDA floors).
- Scoring changes in run 3 fix representation mismatches, not agent behavior. Each is covered by a unit test, including one that guards against matching on generic words.
