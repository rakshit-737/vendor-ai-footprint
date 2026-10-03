# Gold-set evaluation

Gold set: 114 of 114 rows, vendors V-001, V-002, V-003, V-004, V-005, V-006 (`tests/gold/gold_v1.json`, sha256 `48b6a86bdd67`). Fuzzy threshold: partial_ratio >= 90.

> Reported excerpts may be summarised or elided by the scout. They are only search keys for the captured text: they are never evidence and are not reproduced here.

## Gates

| Gate | Target | Result | Status |
|---|---|---|---|
| P2 URL recall | >= 90% of in-scope gold URLs captured or explained | 100.0% | PASS |
| P3 traps | every trap rejected; no citable item rests on a trap | 5 of 5 gold traps rejected or suppressed; 0 item leak(s) | PASS |

## URL recall

| Vendor | Run | In scope | Captured | Explained | Missed | Excluded | Captured or explained |
|---|---|---|---|---|---|---|---|
| V-001 | V-001-20261003-5ddbf3eb | 13 | 12 | 1 | 0 | 0 | 100.0% |
| V-002 | V-002-20261003-877d1823 | 8 | 7 | 1 | 0 | 8 | 100.0% |
| V-003 | V-003-20261003-20fe2885 | 14 | 14 | 0 | 0 | 0 | 100.0% |
| V-004 | V-004-20261003-82674381 | 13 | 7 | 6 | 0 | 0 | 100.0% |
| V-005 | V-005-20261003-05eb23b0 | 9 | 9 | 0 | 0 | 14 | 100.0% |
| V-006 | V-006-20261003-3e6c4f9a | 16 | 15 | 1 | 0 | 0 | 100.0% |
| All |  | 73 | 64 | 9 | 0 | 22 | 100.0% |

### Gold URLs not captured

| Vendor | Rows | URL | Status | Reason |
|---|---|---|---|---|
| V-001 | 16 | https://dir.texas.gov/contracts/vendors/honest-nerd-dba-automworx | explained | logged as a lead, not fetched; seeded for manual capture, not yet imported |
| V-002 | 17, 18, 19 | https://press.aboutamazon.com/aws/2026/5/fiserv-launches-agentos-the-operating-system-for-agentic-ai-in-banking | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-002 | 31 | https://www.fiserv.com/en/insights/articles-and-blogs/what-agentic-ai-means-for-financial-institutions.html | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-002 | 32 | https://www.fiserv.com/en/lp/agentos-by-fiserv.html | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-002 | 33 | https://americanbanker.com/news/fiserv-has-co-created-ai-agents-with-six-banks-and-openai | explained | logged as a lead, not fetched; seeded for manual capture, not yet imported |
| V-002 | 37 | https://www.fiserv.com/content/dam/fiserv-ent/final-files/marketing-collateral/documents-handouts-flyers/2025-sustainability-and-impact-report-0926.pdf | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-002 | 38 | https://careers.fiserv.com/us/en/job/R-10398816 | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-002 | 39 | https://www.fiserv.com/en/about-fiserv/corporate-social-responsibility/champion-responsible-business-practices.html | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-002 | 41 | https://www.fiserv.com/en/insights/articles-and-blogs/core-banking-modernization-4-technologies-shaping-the-future-of-financial-services.html | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-002 | 42 | https://www.fiserv.com/en/solutions/coreadvance.html | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-004 | 58 | https://terrapintech.com/ai-ready-data-wealth-management/ | explained | seed not captured; the seeds collector reported blocked_bot; PRD coverage: seeds blocked_bot, site blocked_bot, wordpress blocked_bot |
| V-004 | 60 | https://terrapintech.com/platform/ | explained | seed not captured; the seeds collector reported blocked_bot; PRD coverage: seeds blocked_bot, site blocked_bot, wordpress blocked_bot |
| V-004 | 65 | https://terrapintech.com/kristefor-lysne-modern-financial-advisor-podcast/ | explained | seed not captured; the seeds collector reported blocked_bot; PRD coverage: seeds blocked_bot, site blocked_bot, wordpress blocked_bot |
| V-004 | 67 | https://terrapintech.com/terrapin-technologies-sponsors-cramer-sales-management-roundtable/ | explained | seed not captured; the seeds collector reported blocked_bot; PRD coverage: seeds blocked_bot, site blocked_bot, wordpress blocked_bot |
| V-004 | 68 | https://terrapintech.com/finra-2025-annual-regulatory-oversight-report-key-takeaways/ | explained | seed not captured; the seeds collector reported blocked_bot; PRD coverage: seeds blocked_bot, site blocked_bot, wordpress blocked_bot |
| V-004 | 70 | https://terrapintech.com/llms.txt | explained | seed not captured; the seeds collector reported blocked_bot; PRD coverage: seeds blocked_bot, site blocked_bot, wordpress blocked_bot |
| V-005 | 71, 72 | https://www.bny.com/content/dam/bnymellon/documents/pdf/insights/modernizing-the-payments-operating-model.pdf | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-005 | 73 | https://www.bny.com/assets/corporate/documents/pdf/investor-relations/earnings/quarterly-update-presentation-1q-2026.pdf | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-005 | 76 | https://www.bny.com/corporate/global/en/solutions/platforms/global-payments-trade/global-payments-solutions/payables/instant-payments.html | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-005 | 77 | https://www.bny.com/corporate/global/en/insights/ai-and-payments-fraud-an-evolving-landscape.html | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-005 | 78 | https://www.bny.com/content/dam/bnymellon/documents/pdf/solutions/BNY-comprehensive-validation-factsheet.pdf | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-005 | 79 | https://www.bny.com/corporate/global/en/solutions/platforms/global-payments-trade/validation-and-protection.html | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-005 | 80 | https://www.bny.com/corporate/global/en/solutions/platforms/global-payments-trade/global-payments-solutions/payables.html | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-005 | 81 | https://www.bny.com/corporate/global/en/solutions/platforms/global-payments-trade/global-payments-solutions/receivables.html | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-005 | 88 | https://www.bny.com/corporate/global/en/about-us/technology-innovation/artificial-intelligence.html | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-005 | 91 | https://www.bny.com/corporate/global/en/about-us/newsroom/company-news/bny-mellon-first-global-bank-to-deploy-ai-supercomputer-powered-by-nvidia-dgx-superpod-with-dgx-h100.html | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-005 | 94 | https://www.bny.com/corporate/global/en/built-in/powered-by-eliza.html | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-005 | 95 | https://eofe.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/BNY-Careers/job/75732 | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-005 | 96 | https://patents.google.com/patent/US20240036963A1/en | excluded | gold family SKIP (outside the collection plan) |
| V-005 | 97 | https://www.bny.com/corporate/global/en/about-us/technology-innovation/artificial-intelligence/project-aikya.html | excluded | manual-only host (terms bar automation); no manual capture for this vendor yet |
| V-006 | 107 | https://payments-forum.americanbanker.com/profile/kyle-caldwell/ | explained | logged as a lead, not fetched; seeded for manual capture, not yet imported |

## Passage recall

| Vendor | Rows in scope | Located | Elsewhere | Partial | Not located | Recall | In a lexicon passage |
|---|---|---|---|---|---|---|---|
| V-001 | 16 | 13 | 1 | 0 | 2 | 87.5% | 11 of 14 |
| V-002 | 16 | 15 | 0 | 0 | 1 | 93.8% | 13 of 14 |
| V-003 | 14 | 14 | 0 | 0 | 0 | 100.0% | 4 of 13 |
| V-004 | 14 | 4 | 0 | 0 | 10 | 28.6% | 1 of 2 |
| V-005 | 12 | 12 | 0 | 0 | 0 | 100.0% | 9 of 11 |
| V-006 | 17 | 16 | 0 | 0 | 1 | 94.1% | 11 of 15 |
| All | 89 | 74 | 1 | 0 | 14 | 84.3% | 49 of 69 |

By gold strength:

| Expect | Rows in scope | Located | Elsewhere | Recall | In a lexicon passage |
|---|---|---|---|---|---|
| strong | 12 | 12 | 0 | 100.0% | 10 of 12 |
| moderate | 30 | 28 | 0 | 93.3% | 21 of 24 |
| weak | 29 | 21 | 1 | 75.9% | 5 of 20 |
| marketing-only | 18 | 13 | 0 | 72.2% | 13 of 13 |

### Rows not located at their own URL

| Row | Vendor | URL | Status | Best score | Reason |
|---|---|---|---|---|---|
| 4 | V-001 | https://labarum.ai/ | not_located | 58.4 | not found in the captured text (best score 58.4); the reported excerpt may be summarised, or the page changed |
| 7 | V-001 | https://www.automworx.com/whats-new-automic-v26-webinar-on-demand/ | not_located | 41.1 | not found in the captured text (best score 41.1); the reported excerpt may be summarised, or the page changed |
| 16 | V-001 | https://dir.texas.gov/contracts/vendors/honest-nerd-dba-automworx | located_elsewhere | 92.0 | found in another captured document of the vendor |
| 33 | V-002 | https://americanbanker.com/news/fiserv-has-co-created-ai-agents-with-six-banks-and-openai | not_located | n/a | URL not captured (explained); not found elsewhere |
| 57 | V-004 | https://terrapintech.com/solutions/data-aggregation-and-management/ | not_located | 46.9 | not found in the captured text (best score 46.9); the reported excerpt may be summarised, or the page changed |
| 58 | V-004 | https://terrapintech.com/ai-ready-data-wealth-management/ | not_located | n/a | URL not captured (explained); not found elsewhere |
| 59 | V-004 | https://terrapintech.com/solutions/compensation-and-commissions/ | not_located | 47.1 | not found in the captured text (best score 47.1); the reported excerpt may be summarised, or the page changed |
| 60 | V-004 | https://terrapintech.com/platform/ | not_located | n/a | URL not captured (explained); not found elsewhere |
| 64 | V-004 | https://terrapintech.com/about/ | not_located | 50.8 | not found in the captured text (best score 50.8); the reported excerpt may be summarised, or the page changed |
| 65 | V-004 | https://terrapintech.com/kristefor-lysne-modern-financial-advisor-podcast/ | not_located | n/a | URL not captured (explained); not found elsewhere |
| 67 | V-004 | https://terrapintech.com/terrapin-technologies-sponsors-cramer-sales-management-roundtable/ | not_located | n/a | URL not captured (explained); not found elsewhere |
| 68 | V-004 | https://terrapintech.com/finra-2025-annual-regulatory-oversight-report-key-takeaways/ | not_located | n/a | URL not captured (explained); not found elsewhere |
| 69 | V-004 | https://www.thewealthadvisor.com/article/terrapin-technologies-launches-trade-surveillance-solution-partnership-wintrust-wealth | not_located | 47.0 | not found in the captured text (best score 47.0); the reported excerpt may be summarised, or the page changed |
| 70 | V-004 | https://terrapintech.com/llms.txt | not_located | n/a | URL not captured (explained); not found elsewhere |
| 107 | V-006 | https://payments-forum.americanbanker.com/profile/kyle-caldwell/ | not_located | n/a | URL not captured (explained); not found elsewhere |

### Located excerpts outside every lexicon passage

These strong, moderate and marketing-only excerpts are in the captured text but in no lexicon passage (passages.jsonl), so rules that read passages never see them.

| Row | Vendor | URL | Expect | Status |
|---|---|---|---|---|
| 3 | V-001 | https://labarum.ai/capabilities/ | moderate | located |
| 12 | V-001 | https://www.automworx.com/webinar-replatforming-to-automic/ | moderate | located |
| 20 | V-002 | https://mondovisione.com/media-and-resources/news/fiserv-forms-strategic-collaboration-with-openai-to-bring-ai-to-how-fiserv-serve-2026514/ | strong | located |
| 75 | V-005 | https://www.theasianbanker.com/updates-and-articles/bny-strengthens-gp-t-execution-through-embedded-ai-and-infrastructure-scale | strong | located |
| 98 | V-006 | https://www.sardine.ai/media/fraud-forward/episodes/payments-fraud-prevention-lifecycle | moderate | located |

## Strength agreement

Matched 60 rows; 21 agree (35.0%). Matched on the excerpt itself: 49, of which 19 agree (38.8%). Rows with no item: 53; rows whose item is a trap: 0.

| Gold expect | Strong | Moderate | Context - relationship only | Context - platform supplier | Context - inferred affiliate | Marketing only | Weak | none |
|---|---|---|---|---|---|---|---|---|
| strong | 0 | 2 | 1 | 3 | 0 | 5 | 1 | 6 |
| moderate | 0 | 7 | 6 | 2 | 3 | 3 | 5 | 12 |
| weak | 0 | 1 | 2 | 1 | 1 | 2 | 2 | 26 |
| marketing-only | 0 | 0 | 0 | 2 | 1 | 8 | 2 | 9 |

## Gemini vs rules

340 of 527 items carry Gemini labels: 89 agree with the rules, 251 disagree (temporal 193, action_level 67, sp 149). Pending proposals: 113.

## Trap checks

Vocabulary: 19 definition-test phrases or patterns, 5 relabelled-automation patterns, 1 trap-source path pattern(s), 13 seeded collision name(s) and 1 colliding SEC CIK(s).

| Row | Vendor | URL | Kind | Trap phrase | Status |
|---|---|---|---|---|---|
| 47 | V-003 | https://www.fssi-ca.com/press-room/fssi-honored-as-printing-impressions-2025-innovator-of-the-year/ | definition | intelligent tools | suppressed |
| 54 | V-003 | https://www.fssi-ca.com/infrastructure/ | definition | intelligent inserting | suppressed |
| 70 | V-004 | https://terrapintech.com/llms.txt | definition | llms.txt, meant for consumption by LLMs, consumption by LLMs, this is an llms.txt file | suppressed |
| 70 | V-004 | https://terrapintech.com/llms.txt | source | llms.txt | suppressed |
| 81 | V-005 | https://www.bny.com/corporate/global/en/solutions/platforms/global-payments-trade/global-payments-solutions/receivables.html | definition | AI-driven automation | suppressed |

Definition-test trap items in the findings: 9, rejected: 9.

No citable item fails the definition, source or entity trap tests.
