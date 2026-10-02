# Manual capture checklist (P2)

Some sources may not be collected by the tool:

- **fiserv.com** and its subdomains. Fiserv's Terms of Use (s.7) bar spiders and data mining.
- **LinkedIn**. Its User Agreement bars scraping.
- **Bot-protected pages**: openai.com customer stories (Cloudflare) and the Texas DIR vendor page.
- **robots.txt-barred files**: TCH `/-/media/` PDFs.

A person may still view these public pages in a normal browser and keep a copy as evidence. This checklist covers
them. It takes about 45 minutes.

## Rules (from the brief and our method)
- Use a normal browser, logged out. Do not log in, fill forms, download gated files or contact anyone.
- On LinkedIn, only open public post URLs while logged out. If a login wall appears, skip the item. Never react,
  follow, connect or message anyone.
- Capture what you see. Do not edit the files.

## How to capture each item
For every URL below:
1. Open the page and let it finish loading.
2. Save the page source:
   - HTML pages: **Ctrl+S → "Webpage, HTML only"**.
   - PDFs: download the PDF as-is.
3. Take a screenshot of the passage that matters, or a full-page screenshot. Chrome/Edge full-page capture:
   DevTools → Ctrl+Shift+P → "Capture full size screenshot".
4. Put both files in `evidence/manual_inbox/<VENDOR-ID>/`, named `NN-short-name.html` (or `.pdf`) and
   `NN-short-name.png`.
5. Add one line per item to `evidence/manual_inbox/<VENDOR-ID>/captures.csv`:

```
nn,url,file,screenshot,captured_at_local,analyst,note
01,https://www.fiserv.com/en/lp/agentos-by-fiserv.html,01-agentos.html,01-agentos.png,2026-10-06 14:05,RK,agentOS landing page
```

`footprint capture import` (built in P2) hashes the files, records who captured them and when, and adds them to
the evidence store. Hashes are taken on import, so do not change the files after saving them.

## V-002 Fiserv (Critical tier, sampling protocol)
| # | Family | What | URL / how to find it |
|---|---|---|---|
| 01 | PRD | agentOS landing page | https://www.fiserv.com/en/lp/agentos-by-fiserv.html |
| 02 | PRD | Agentic AI insights article | https://www.fiserv.com/en/insights/articles-and-blogs/what-agentic-ai-means-for-financial-institutions.html |
| 03 | PRD | Core banking modernisation article | https://www.fiserv.com/en/insights/articles-and-blogs/core-banking-modernization-4-technologies-shaping-the-future.html (find via site search if the URL changed) |
| 04 | PRD | CoreAdvance page | https://www.fiserv.com/en/solutions/coreadvance.html |
| 05 | PRD | DNA product page (negative finding if it never mentions AI) | fiserv.com → Solutions → Account Processing → DNA |
| 06 | PRD | CSR "responsible business practices" page | https://www.fiserv.com/en/about-fiserv/corporate-social-responsibility/champion-responsible-business-practices.html |
| 07 | PRD | 2025 Sustainability & Impact Report (PDF) | linked from the CSR pages (look for "generative AI usage compliance policy") |
| 08 | LEG | Privacy notice | https://www.fiserv.com/en/about-fiserv/privacy-notice.html |
| 09 | LEG | Terms of use (shows the s.7 clause that makes this manual) | footer → Terms of Use |
| 10 | LEG | Any footer page on security, trust, responsible AI or sub-processors | footer links. If none exist, write "none found" in `captures.csv` |
| 11-14 | PRD | Site search results pages for: `AI`, `machine learning`, `generative AI`, `agentic` | the fiserv.com search box. Capture the first results page for each |
| 15-17 | JOB | careers.fiserv.com keyword searches: `machine learning`, `artificial intelligence`, `generative AI` | Capture each results page (shows the result count) |
| 18-27 | JOB | The 10 most recent matching postings, preferring account processing / core banking / DNA teams | open each posting from the searches above |
| 28 | NEWS | 14 May 2026 agentOS / OpenAI press release on newsroom.fiserv.com, if it opens in your browser | otherwise skip: the SEC copy is collected automatically |

## V-005 BNY (Critical tier)
| # | Family | What | URL |
|---|---|---|---|
| 01 | IND | OpenAI customer story "BNY builds AI for everyone, everywhere" | https://openai.com/index/bny/ |
| 02 | EXEC (optional) | Public LinkedIn posts by BNY payments or AI executives about AI in payments / RTP | logged-out public post URLs only |

## V-006 The Clearing House (Critical tier)
| # | Family | What | URL |
|---|---|---|---|
| 01 | LEG | RTP Document Library schedules / operating rules PDFs that mention fraud or risk signals (robots.txt bars automated download; viewing is fine) | https://www.theclearinghouse.org/payment-systems/rtp → Document Library |
| 02 | EXEC (optional) | Public posts or talks by TCH fraud or product executives about AI | logged-out public URLs only |

## V-001 AutomWorx (High tier, entity resolution)
| # | Family | What | URL |
|---|---|---|---|
| 01 | IND | Texas DIR vendor record "Honest Nerd dba AutomWorx" (Cloudflare blocks scripts) | https://dir.texas.gov/contracts/vendors/honest-nerd-dba-automworx |

If an item will not open, or needs a login, skip it and write the reason in `captures.csv`. The skip is
recorded as a gap in the Coverage Log. A gap is acceptable; an invented capture is not.
