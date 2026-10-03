# P2 collection report: second live run (3 Oct 2026)

Confidential: Meridian TPRM case study. Internal working document of a private repository; never publish.

This report covers the P2 collection-quality pass. It diagnoses the first live run (2 Oct 2026, `runs/V-00x-20261002-*`), lists the fixes and their regression tests, records the verification of the terms-of-use register, and gives the results of the polite live re-run (`as_of` 2026-10-03, mode `live_rules`).

## 1. Result in one table

| Gate (design §Phases, P2) | Result |
|---|---|
| Robots and ToS violations | **0**. All 428 automated captures of the six runs were re-checked offline against the robots.txt body each one recorded (the bodies are now kept in the evidence store) and against `config/tou.toml`. No capture came from a host with `automation = "none"`, and no per-host ceiling was exceeded. |
| Every mandatory family has a status | **Yes.** Each run has one status per family in `family_status.jsonl` (precedence rule in §4), and the per-collector rows are kept in `coverage.jsonl`. |
| Gold URLs captured or explained (excluding manual-only hosts, DNS rows and SKIP) | **68 of 68 (100%)**: 59 captured (86.8%) and 9 explained. Target ≥ 90%. |
| Missed strong/moderate gold URLs on hosts we may collect from automatically | **1** (americanbanker.com, seeded for manual capture). The other 13 strong/moderate URLs not captured are on hosts whose terms bar automation (fiserv.com, bny.com, press.aboutamazon.com). They wait for manual capture (§7). |

Run ids are the newest run for each vendor, which `evaluate.gold_report` picks automatically:

| Vendor | Run id | Tier | Captures | Documents | AI passages | Mandatory family status |
|---|---|---|---|---|---|---|
| V-001 AutomWorx | `V-001-20261003-5ddbf3eb` | High | 79 | 127 | 178 | LEG done, REG n/a, PRD done, JOB n/a, DNS done, IND done |
| V-002 Fiserv | `V-002-20261003-877d1823` | Critical | 28 | 13 | 160 | LEG, PRD, JOB awaiting manual capture; REG stopped (rule); DNS, IND, HIST done |
| V-003 FSSI | `V-003-20261003-20fe2885` | High | 48 | 30 | 30 | LEG done, REG n/a, PRD stopped (host ceiling), JOB, DNS, IND, HIST done |
| V-004 Terrapin | `V-004-20261003-82674381` | High | 27 | 8 | 8 | LEG, PRD **blocked_bot** (site refuses our User-Agent); REG n/a, JOB n/a, DNS, IND done; HIST done (on lead) |
| V-005 BNY | `V-005-20261003-05eb23b0` | Critical | 38 | 16 | 103 | LEG, PRD, JOB awaiting manual capture (terms bar automation, §5); REG stopped (rule); DNS, IND, HIST done |
| V-006 TCH | `V-006-20261003-3e6c4f9a` | Critical | 208 | 178 | 34 | LEG done, REG n/a, PRD stopped (cap), JOB, DNS, IND, HIST done |

On 2 Oct V-006 had 10 passages, and only one of them came from a job posting. On 3 Oct its 25 posting documents give 22 passages. All 7 TCH postings that name AI now give passages (the gold set expects about 7), including the NOC Operations Manager posting's line on AI tools (Microsoft Copilot, ChatGPT, ServiceNow AI).

## 2. First-run problems: cause, fix, regression test

| # | Symptom (2 Oct runs) | Cause found in `runs/` and `evidence/` | Fix | Regression test |
|---|---|---|---|---|
| 1 | V-004 Terrapin: LEG/PRD `error`, only 6 passages | terrapintech.com returns `403 - Forbidden` with a 52-byte body to our declared User-Agent. This applies to every path, robots.txt included; curl's default agent gets 200. That is a refusal of the client, not a missing page. We kept sending all 13 requests anyway. | `LiveFetcher` reports a refusal as `blocked_bot`. A refusal is a robots.txt 401/403 followed by a page 401/403, or 3 consecutive 401/403 answers from one host. The body is kept as evidence (note `host_refused`) and no further request goes to that host in the run. The User-Agent is never changed (no evasion). Refused pages become a Wayback lead: High-tier HIST is `on_lead`, so archive copies are read with the discretionary budget (16 of 50 used). The site collector marks LEG `blocked_bot` too, instead of claiming a "no legal page found" negative. | `test_host_refusing_declared_ua_is_blocked_bot_and_not_hammered`, `test_consecutive_refusals_open_the_circuit`, `test_replay_reproduces_host_refusal_as_blocked_bot`, `test_wayback_on_lead_replays_only_refused_pages_from_discretionary_budget`, `test_home_refused_marks_both_families_blocked_and_skips_sitemap` |
| 2 | V-006 TCH: 237 captures but 10 passages; Workday postings barely used | (a) Seeded Workday, Oracle and Workable career pages are script shells, so they gave **empty** documents (text_len 0). (b) `html_to_text` joined `<p>`/`<li>` blocks with no break ("improvement.The successful"), so a whole posting was one 6,000-character "sentence". (c) `find_passages` cut an over-long sentence to its first 700 characters, so later AI mentions (the NOC Manager's Copilot/ChatGPT line) were lost. (d) `AI-powered` did not match `AI`: the lexicon regex forbade a trailing hyphen. (e) Postings whose titles missed the old slug filter (a paralegal, business development) were never read. | (a) Seeds on the ATS host are deferred to the jobs collector, which reads them through the ATS JSON API with `seeded=True`. (b) `extract.html_fragment_text` keeps one block per paragraph, list item or line break. (c) An over-long sentence gets one ≤700-character window per hit cluster, on word boundaries. (d) Core terms also match at the head of a hyphen compound and in the plural (as the lexicon's `[ai_terms]` note says the tagger does); `non-AI` still does not match. (e) Title gate = the design's role keyword list (AI terms, service terms, NOC, SRE, operations, engineering, data, fraud, support…) plus every seeded posting. | `test_workday_seeded_posting_read_through_json_api`, `test_seeds_defer_ats_postings_to_jobs_collector`, `test_html_fragment_text_keeps_block_breaks`, `test_long_sentence_windows_follow_every_hit`, `test_hyphen_compounds_and_plurals_count` |
| 3 | DNS `error` for V-002 (fiserv.com) and V-005 (bnymellon.com) | Cloudflare answers TXT in RFC 1035 presentation format. Non-ASCII bytes come as `\DDD` escapes (`\226\128\156…`), while Google returns decoded UTF-8. Two records with curly quotes or Cyrillic letters therefore "disagreed". | `dns.unescape_txt` decodes quoted character-strings and `\DDD` / `\X` escapes before the two resolvers are compared. Both domains are now `done`. | `test_parse_doh_decodes_cloudflare_decimal_escapes` |
| 4 | Several coverage rows per family (V-005 had two DNS rows; PRD had seeds, site and WordPress) | Each collector writes its own row, and no single family status existed. | `pipeline.aggregate_coverage` gives one row per vendor and family (collector `family`) with a documented precedence (§4). The rows go to `family_status.jsonl` and the manifest; the per-collector rows stay in `coverage.jsonl`. The CLI `coverage` command shows the family rows first, and `sheets.coverage_log_sheet(entries, summary=...)` prints both. | `test_family_status_precedence`, `test_aggregate_coverage_one_row_per_family_keeps_details`, `test_coverage_log_with_family_summary_rows` |
| 5 | V-002 JOB `pending` (bare) | Fiserv's careers site is manual-only, so the collector wrote "done_manual pending". There was no path to `done_manual`, and manual captures never reached a run. | Manual-only families now read **`pending` with note "awaiting manual capture"** (the CLI shows `awaiting_manual`). They become **`done_manual`** once analyst captures are imported. A new `ManualCollector` extracts every imported capture of the vendor into the run (no network), with a `done_manual` row per family naming the analysts. The site collector writes the same status for LEG/PRD on a terms-barred home page. | `test_jobs_none_and_manual`, `test_jobs_manual_is_done_manual_once_postings_are_imported`, `test_seeds_manual_seed_already_imported_is_done_manual`, `test_manual_collector_extracts_imported_captures`, `test_terms_barred_site_awaits_manual_capture_without_requests` |
| 6 | Found while diagnosing: SEC 10-K/10-Q never extracted (V-002 and V-005 REG) | Inline-XBRL filings start with `<?xml … encoding='ASCII'?>`, and `lxml` refuses a str that carries an encoding declaration. `to_document` swallowed the error, so the 2.7 MB 10-K was "fetched, 0 documents". | The XML declaration is stripped before parsing. V-002 REG now has 11 documents and 142 passages. | `test_xml_declared_ixbrl_html_is_extracted`, `test_sgml_wrapped_sec_document_falls_back_to_blocks` |
| 7 | Found: every HTML page of the first run used the lxml fallback; Elementor `<div>` copy was lost | The first run fell back to lxml for all HTML (trafilatura was unusable in that environment), and the fallback only read `p/li/h*/td`. trafilatura itself also skips body copy kept in bare `<div>`s (labarum.ai). | A live run now refuses to start without trafilatura, protego and pypdf (`pipeline.check_live_dependencies`). trafilatura stays primary. The new block walker (`extract.html_blocks`: every block element, nav/header/footer/cookie chrome pruned) replaces it when trafilatura returns under 200 characters or dropped an AI-bearing block of 8 or more words. | `test_div_only_text_is_not_lost`, `test_live_dependency_check` |
| 8 | Found: LEG cap spent on news and courses; privacy notices never fetched (TCH, BNY) | Legal detection used substrings: "trust" matched trustee news, and "compliance"/"security" matched ACH training pages. TCH's PRD cap then went on ACH course and shop pages whose slugs carry the service term. | Legal pages are judged on whole path words and segments, never under news, blog or education sections. Course, shop and event sections rank last. Articles, insights and blog sections get a small bonus, and newer sitemap `lastmod` comes first among equal scores. | `test_is_legal_uses_whole_words_not_substrings`, `test_training_and_shop_pages_rank_after_articles`, `test_sitemap_lastmod_puts_newest_pages_first` |
| 9 | Found: V-006 IND icba.org `network_error`; Wayback CDX `network_error` | icba.org answers after about 29 s, but the read timeout was 20 s. | Timeouts are now (10 s connect, 60 s read), and a timeout or connection error is retried once with back-off. | `test_slow_server_timeout_is_retried_once` |
| 10 | Found in the re-run: one dropped connection to `www.sec.gov/robots.txt` barred every SEC request of a run | `RobotsCache` treats a network error as disallow-all for the whole run (correct per RFC 9309). The robots fetch was not retried. | The robots.txt fetch is retried twice (timeouts, connection errors, 429/5xx, honouring Retry-After) before disallow-all applies. | `test_robots_fetch_is_retried_before_disallowing_everything` |
| 11 | Found: FSSI's 25-request host ceiling produced 102 refused attempts | The site crawl kept asking after the ToS register answered `cap_reached`, and refused attempts were charged to the family budget. | The crawl stops at the host ceiling and logs the rest as leads. Fetches refused before any request (`cap_reached`, `blocked_tou`, `blocked_robots`) no longer use family budget (design 2.5: robots and terms checks are not counted). | `test_host_ceiling_stops_the_crawl_and_refusals_cost_no_budget`, `test_context_fetch_dedupes_urls_and_records_blocks` |

Other changes:
- **One request per URL per vendor run.** `CollectContext.fetch` returns the first outcome for a repeated URL. The site collector no longer refetches the seeds' pages, and the SEC collector no longer refetches seeded filings.
- **Fetch log.** `runs/<id>/fetch_log.jsonl` records one line per gate decision: ToS register, robots.txt (status and sha256), GET, refusal circuit and retries. The manifest carries the decision counts.
- **robots.txt bodies.** Each body is stored in `evidence/blobs` by its sha256 (`EvidenceStore.put_blob`), so every capture's `robots_sha256` can be re-checked offline. The audit in §1 did exactly this.
- **24-month rule.** Job postings older than 24 months (posted date against `as_of`) are skipped (design 2.5).
- **Unit tests.** All of them stay offline (local `http.server` threads and in-memory fakes).

## 3. Terms-of-use register verification (`config/tou.toml` v1.1)

Every entry's terms page was read on 3 Oct 2026 with WebFetch. Script-rendered pages that WebFetch could not read were read from a Wayback copy with curl. The governing clause is quoted in each entry's `note`, and `verified = true` is set, except for Broadcom (see open issue 4). Rule applied: **terms that bar automated access → `automation = "none"`**.

| Entry | Decision | Governing clause (abridged; full quote in the register) |
|---|---|---|
| fiserv.com, fiserv.wd5.myworkdayjobs.com | none (unchanged) | Terms s.7: "may not employ any spiders, robots or other similar data mining programs" |
| **bny.com** | **full → none** | "You may not use any robot, spider, intelligent agent, other automatic device or manual process, or automated systems to search, monitor or copy our web pages, data, content…" |
| **eofe.fa.us2.oraclecloud.com** (BNY careers) | **full → none** | BNY postings are BNY content under the same Terms of Use. `seeds/V-005.toml` `ats.platform = "manual"` (reader kept as `reader = "oracle_orc"`). |
| **press.aboutamazon.com** | **limited → none** | The footer links Amazon's Conditions of Use, whose licence excludes "any use of data mining, robots, or similar data gathering and extraction tools". |
| openai.com, linkedin.com | none (unchanged) | OpenAI: "Automatically or programmatically extract data or Output". LinkedIn s.8.2: "…robots or any other means or processes… to scrape or copy the Services". |
| theclearinghouse.org | full, **max_rps 1.0 → 0.5** | s.7.1(9) bars automated systems sending more requests than "a human can reasonably produce … by using a conventional browser". |
| theclearinghouse.wd108.myworkdayjobs.com | full, **max_rps 0.8 → 0.5** | The Workday Site Terms bar data mining on "our Sites", but they cover only pages that reference them, and the TCH career site references no terms. TCH's human-rate clause applies. |
| fssi-ca.com | full | No automated-access clause. The use licence allows only "personal, non-commercial transitory viewing" (open issue 3). |
| automworx.com, labarum.ai, terrapintech.com | unchanged | No terms of use published (privacy policies only; labarum.ai shows "Terms" without a link). |
| sec.gov | full | "Current max request rate: 10 requests/second"; "Please declare your user agent"; no crawling. We use 5 req/s, a declared UA with a contact address, and targeted queries. |
| archive.org | full | "granted for scholarship and research purposes only". Never Save Page Now. |
| dns.google, cloudflare-dns.com | full | Google APIs ToS (request limits; no "permanent copies" of API content, see open issue 3). The 1.1.1.1 page states no usage restriction. |
| apply.workable.com | full | The customer terms have no visitor-scraping clause. |
| mondovisione.com, theasianbanker.com, googlecloudpresscorner.com, api.github.com | unchanged | No automated-access bar. Content-reuse limits are noted. Google ToS: automated access only within robots.txt. GitHub: API use is not scraping. |
| automation.broadcom.com, ftpdocs.broadcom.com | limited, **verified = false** | The Broadcom terms page is script-rendered, and neither WebFetch, curl nor three Wayback copies returned its text. A human must read it in a browser. |

## 4. Coverage: family status precedence

`pipeline.FAMILY_STATUS_PRECEDENCE` decides one status per family from its collector rows:
1. If any row is complete, the family is complete: `done_manual` > `stopped` > `done`. A cap or saturation rule that left part of a family unread outranks `done`.
2. Otherwise the incomplete status, in the order `pending` (awaiting manual capture, the planned next step) > `blocked_tou` > `blocked_robots` > `blocked_bot` > `error` > `descoped`.
3. `not_applicable` applies only when every row is `not_applicable`. A secondary collector that does not apply, such as the WordPress sweep on a non-WordPress site, never decides a family on its own.

Requests, documents and AI passages are summed per family. The detail rows stay unchanged, so request totals in column N are never double counted.

## 5. Blocked items and their reasons (from the runs' fetch logs)

| Vendor | Item | Decision | Reason |
|---|---|---|---|
| V-002 | fiserv.com (site, WordPress, 6 PRD seeds, JOB seed, careers) | not requested | Terms s.7 (manual only): awaiting manual capture |
| V-002 | press.aboutamazon.com agentOS release; americanbanker.com article | not requested | Amazon Conditions of Use (none); americanbanker seed is manual. Both are leads awaiting manual capture. |
| V-004 | terrapintech.com and www.terrapintech.com (robots.txt, home, 10 seeds, wp-json) | blocked_bot | `host_refused`: robots.txt HTTP 403 and page HTTP 403 for the declared User-Agent. 2 page requests and 2 robots.txt requests reached the host; 10 more were stopped by the circuit. Wayback: 5 pages replayed (home, about, security capabilities and 2 solution pages). There is no snapshot of the AI-ready-data post, the platform page, the 3 blog posts or llms.txt (the CDX index is empty for them too). |
| V-005 | bny.com (site, WordPress, 12 seeds), BNY Oracle careers (JOB) | not requested | BNY Terms of Use (none): awaiting manual capture |
| V-005 | www.bnymellon.com/wp-json/ | blocked_tou | The host is not in the register, so the default (seeded URLs only) applies |
| V-001 | www.labarum.ai/wp-json/ | blocked_tou | labarum.ai is `limited` (seeded pages only), so the WordPress sweep was refused |
| V-003 | fssi-ca.com beyond 25 requests | cap_reached | Per-run host ceiling (ToS register). PRD and LEG are `stopped` and the remainder is logged as leads. |
| V-001, V-006 | dir.texas.gov; payments-forum.americanbanker.com | not requested | Seeded for manual capture |

No request was refused by robots.txt in the final runs. thewealthadvisor.com, refused by robots on 2 Oct, was allowed on 3 Oct under its current robots.txt.

## 6. Gold recall (`tests/gold/gold_v1.json`, 95 unique URLs)

Scope: 21 URLs are on manual-only hosts (fiserv.com and careers.fiserv.com 7, bny.com 12, the BNY Oracle careers page 1, press.aboutamazon.com 1) and none has a manual capture yet. 5 rows are DNS lookups and 1 is a patent (SKIP). That leaves **68 URLs**.

| Vendor | In scope | Captured | Explained | Captured or explained |
|---|---|---|---|---|
| V-001 | 13 | 12 | 1 | 100% |
| V-002 | 7 | 6 | 1 | 100% |
| V-003 | 13 | 13 | 0 | 100% |
| V-004 | 12 | 6 | 6 | 100% |
| V-005 | 8 | 8 | 0 | 100% |
| V-006 | 15 | 14 | 1 | 100% |
| **All** | **68** | **59 (86.8%)** | **9** | **100%** |

All 5 DNS rows are captured, as DNS documents of the same domain. The `evaluate.gold_report` gate figure, which counts DNS rows, is 73 in scope, 64 captured (87.7%), 100% captured or explained, gate passed.

Passage recall: gold excerpts located in captured text went from 68.9% (2 Oct, 106 rows in scope) to 84.3% (3 Oct, 89 rows in scope). The scope changed because the BNY and Amazon rows are now manual-only. Of the located excerpts, 49 of 69 now sit inside an extracted AI passage (2 Oct: 38 of 67).

**Strong or moderate gold URLs not captured, with causes**

| Vendor | Expect | URL | Cause |
|---|---|---|---|
| V-002 | moderate | americanbanker.com/news/fiserv-has-co-created-ai-agents-with-six-banks-and-openai | Seeded `automation = "manual"`; awaiting manual capture |
| V-002 | strong | press.aboutamazon.com/aws/2026/5/fiserv-launches-agentos… | Amazon Conditions of Use bar robots (register `none` since 3 Oct); manual capture needed |
| V-002 | moderate ×4 | fiserv.com agentic-AI article, agentOS landing page, 2025 sustainability report PDF; careers.fiserv.com R-10398816 | fiserv.com Terms s.7; manual sampling protocol |
| V-005 | strong ×2 | bny.com payments operating-model whitepaper PDF; 1Q 2026 investor presentation PDF | BNY Terms of Use bar automated access (since 3 Oct); manual capture needed |
| V-005 | moderate ×4 | bny.com instant-payments page, AI-and-payments-fraud insight, AI hub page, NVIDIA DGX SuperPOD newsroom item | same |

Two strong excerpts were captured but sit outside any AI passage, because the sentences name a provider but no `[core]` term. One is the mondovisione.com Fiserv/OpenAI release (the agents are developed "with OpenAI"); the other is the Asian Banker GP&T interview ("models from leading providers such as OpenAI, Google and Anthropic"). Their passages need a lexicon change (handoff to the rules owner, §8).

## 7. Manual capture still needed (for done_manual and the remaining gold)

- **Fiserv (V-002), manual sampling protocol (design 2.5):**
  - LEG: privacy, terms, and the trust/security/AI pages linked from the footer.
  - PRD: the 6 seeded pages, plus the site search for AI, machine learning, generative AI and agentic.
  - JOB: the keyword search counts and the 10 most recent matching postings.
  - Also: the AWS press-centre agentOS release and the American Banker article.
- **BNY (V-005), the same protocol (new since 3 Oct):**
  - The 12 seeded bny.com pages and PDFs (2 strong).
  - LEG pages.
  - The Oracle careers keyword search (BNY-Careers).
  - BNY's terms also forbid a "manual process" to "search, monitor or copy" its pages; see open issue 1 before capturing.
- **Terrapin (V-004):** the AI-ready-data post, the platform page, the three blog posts and the security page, captured with an ordinary browser. The site refuses only automated clients that identify themselves.
- **Others:** dir.texas.gov (V-001) and payments-forum.americanbanker.com (V-006).

Import with `footprint capture import --analyst <initials>`. The next `footprint collect` run turns these families into `done_manual` with no automated request.

## 8. Open issues and handoffs

1. **BNY evidence from 2 Oct.** 271 bny.com, bnymellon.com and BNY Oracle captures from the first run (`runs/V-005-20261002-1ecdaaf4`, still in `evidence/`) were made before the register was verified, and BNY's terms bar that access. The evidence index is append-only, so nothing was deleted, and the 3 Oct run does not use those captures. The team must decide whether to purge or quarantine them, and whether manual capture under BNY's "manual process" wording is acceptable for vendor due diligence.
2. **Content-use clauses.** FSSI ("personal, non-commercial transitory viewing", destroy downloaded materials), Mondo Visione (no storage "in retrieval system" without consent) and the Google APIs ToS (no "permanent copies" of API content; our DNS documents store public TXT records) restrict keeping copies. The team should decide on evidence retention for the submission.
3. **TCH load.** TCH was crawled three times on 3 Oct: the first pass, then two re-runs to bring V-002, V-005 and V-006 onto the final code after the robots retry fix. That was about 460 GETs at ≤0.5 req/s, within s.7.1(9). Future runs should crawl a vendor once per day.
4. **Broadcom terms unverified.** Two entries keep `verified = false`; someone must read https://www.broadcom.com/company/legal/terms-of-use in a browser.
5. **Lexicon (rules owner: `config/lexicon.toml`).** P2 passage finding uses `[core]` only, and these gold sentences fall outside every passage:
   - "agents … with OpenAI" (mondovisione.com, strong);
   - "models from leading providers such as OpenAI, Google and Anthropic" (theasianbanker.com, strong);
   - "Open-source large language models … retrieval-augmented generation" (labarum.ai, moderate).

   Request: add `OpenAI`, `Anthropic`, `ChatGPT` (case-sensitive) and the phrases `large language model` and `retrieval-augmented generation` to `[core]`, or say that P2 should also read `[ai_terms]` and unguarded `[[provider]]` names. Also consider moving `RAG` to `case_sensitive`, since the case-insensitive phrase list matches the word "rag".
6. **SEC filing limit.** The SEC collector reads the 8 newest filing documents per run (`MAX_PRIMARY_DOCS`) and logs the rest as leads, which leaves REG at `stopped (rule)`. The gold REG filings are seeded and captured. Raising the limit toward the REG cap (80/40) is a depth-policy choice.
7. **Contract updates outside my files:**
   - `docs/contracts_p2.md` should list the new outputs (`family_status.jsonl`, `fetch_log.jsonl`), `EvidenceStore.put_blob/get_blob/manual_captures`, the `blocked_bot` host refusal, `CollectContext.as_of/blocked/discretionary`, and the manual collector.
   - `depth._family_status` treats any complete row as complete. It agrees with the precedence above except for a lone `not_applicable` among failed rows; using `pipeline.family_status` would close that gap.
