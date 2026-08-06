# RAG Ingestion Data — Source Manifest

Gathered 2026-07-21 from the links in `docs/rag_source_links_catalog.pdf` (Tier 1 —
ZATCA Official + SOCPA only; Tier 2 GCC and Tier 3 interpretive sources were
explicitly out of scope per the catalog's own ingestion policy). This file
documents exactly where each item in this folder came from, and what from
the catalog could **not** be gathered, so nothing here is treated as
"complete" without knowing its real provenance and gaps.

## Successfully gathered (16 files)

### Direct downloads — unchanged from source, real file format

| File | Source URL | Notes |
|---|---|---|
| `vat_implementing_regulations_full_text.pdf` | `https://zatca.gov.sa/en/RulesRegulations/Taxes/Documents/Implmenting%20Regulations%20of%20the%20VAT%20Law_EN.pdf` | **Not in the original catalog** — the catalog only had a landing page for this. Found via search; this is the actual regulation text. |
| `gcc_unified_vat_agreement_ar.pdf` | `https://zatca.gov.sa/ar/RulesRegulations/Taxes/Documents/GCC%20VAT%20Agreement.pdf` | **Not in the original catalog** — same situation, catalog only had a landing page. Arabic version. |
| `amendments_to_implementing_regulation_vat.pdf` | catalog URL, unchanged | |
| `xml_implementation_standard_v1.2_2023_current_ar.pdf` | catalog URL, unchanged | Arabic, marked CURRENT per catalog |
| `xml_implementation_standard_2022_superseded_ar.pdf` | catalog URL, unchanged | Arabic, marked SUPERSEDED per catalog — set `is_current=false` at ingestion |
| `security_features_implementation_standard_2022_ar.pdf` | catalog URL, unchanged | Arabic |
| `detailed_einvoicing_guideline_fatoora.pdf` | catalog URL, unchanged | |
| `implementing_regulations_zakat_collection.pdf` | catalog URL, unchanged | Effective for financial years after 1/1/2024 — see version metadata note below |
| `guideline_for_zakat_documents.pdf` | catalog URL, unchanged | |
| `zakat_collection_overview_publication.pdf` | catalog URL, unchanged | |
| `electronic_invoice_data_dictionary_2023_current.xlsx` | `https://zatca.gov.sa/ar/E-Invoicing/SystemsDevelopers/Documents/20230519_EInvoice_Data_Dictionary%20vF.xlsx` | Catalog only linked the specifications *page*; this is the actual current Data Dictionary file, found by inspecting that page. XLSX, not PDF. |

### Converted from live web page content (not a downloadable file at source)

All four of these were rebuilt from **raw HTML parsed directly** (BeautifulSoup),
not from an AI-summarized pass — see the methodology note below for why that
distinction matters.

| File | Source URL | Format chosen | Notes |
|---|---|---|---|
| `vat_penalties_taxation_violation_fines.csv` | `https://zatca.gov.sa/en/RulesRegulations/VAT/Pages/Penalties.aspx` | CSV | Real penalty table, transcribed from the live page. **Caveat not captured in the CSV data itself**: the page states repeated violations within three years may result in doubled fines — a general modifier, not a per-row figure. |
| `vat_hub_overview.pdf` | `https://zatca.gov.sa/en/RulesRegulations/VAT/Pages/default.aspx` | PDF (verbatim extracted text) | Definition of VAT + the exact wording of the 3 SME compliance simplifications (annual return filing under SAR 40M, simplified invoicing under SAR 1,000 with Arabic-language requirement, cash basis accounting under SAR 5M). Page's own "Last Update": 01 Aug 2025. |
| `einvoicing_hub_overview.pdf` | `https://zatca.gov.sa/en/E-Invoicing/Pages/default.aspx` | PDF (verbatim extracted text) | Definition + **rollout phase dates** (Phase 1: 4 Dec 2021, Phase 2: 1 Jan 2023) — this detail was missed in the first pass and only surfaced on the raw-HTML re-check. Page's own "Last Update": 15 Dec 2025. |
| `zakat_regulations_hub_version_metadata.pdf` | `https://zatca.gov.sa/en/RulesRegulations/Taxes/Pages/ZakatRegulations.aspx` | PDF (verbatim extracted text) | Version/effective-date metadata for the Zakat Collection regulation. Page's own "Last Update": 01 Jun 2025. |
| `socpa_accountants_regulations.pdf` | `https://socpa.org.sa/Socpa/About-Socpa/Accountant-s-Regulations.aspx?lang=en-us` | PDF (generated) | **Complete verbatim text of all 38 articles**, extracted directly from raw HTML. Page last updated 01 Jul 2021 per the source page itself. |

## NOT gathered — needs Mohammed's manual access (login-gated or otherwise blocked)

These are **not dead links** — they're real pages that automated fetching can't get past. Listed here so you can try them yourself with whatever access you have:

**SOCPA's actual endorsed Accounting Standards — gated behind SOCPA's SSO login for every sub-page tested:**
- Accounting Standards Endorsed: `https://socpa.org.sa/Socpa/Professional-standards/Accounting-standards/Endorsed.aspx?lang=en-us`
- Accounting Standards Endorsed (on-line copy): `https://socpa.org.sa/Socpa/Professional-standards/Accounting-standards/hard-copy.aspx?lang=en-us`
- Standards Updates (SMEs): `https://socpa.org.sa/Socpa/Professional-standards/Accounting-standards/smes.aspx?lang=en-us`
- Endorsed Amendments to Accounting Standards: `https://socpa.org.sa/Socpa/Professional-standards/Accounting-standards/Updates-based-on-accounting-standards-and-audi.aspx`
- National Standards and Technical Opinions: `https://socpa.org.sa/Socpa/Professional-standards/Accounting-standards/Technical-standards-and-mechanisms-that-comple.aspx`
- Updates to Accounting Standards for Non-profitable Organizations: `https://socpa.org.sa/Socpa/Professional-standards/Accounting-standards/2514.aspx`
- Archive of Previous Standards: `https://socpa.org.sa/Socpa/Professional-standards/Accounting-standards/Accounting-Standards.aspx`
- (First 3 confirmed login-gated directly; the remaining 4 weren't individually tested once the pattern was clear across all 3, but expect the same.)

**Accessible but Arabic-only (no English version published):**
- Accounting Standards for Non-profit Organizations: `https://socpa.org.sa/Socpa/Professional-standards/Accounting-standards/Non-profitable-organizations.aspx?lang=en-us` — page explicitly says "This page is not available in English, please click here for the Arabic version." Not pursued further — would need the Arabic URL, not yet identified.

**This is a real, meaningful gap**: SOCPA's actual accounting standards (the IFRS-endorsed technical standards) are not publicly downloadable at all through what was checked — only the *Accountants' Regulations* (licensing/professional conduct rules) was public. If SOCPA accounting standards content is needed for v1, it requires either a SOCPA login (if Mohammed or Mizan.ai has one) or another distribution channel.

## NOT gathered — confirmed dead links (5 of the original 13 direct-PDF catalog entries)

These returned ZATCA's soft-404 ("Not found page", HTTP 200 with a redirect to `PageNotFound.aspx`) at their catalogued URL. Tried the catalog URL plus several plausible alternate path patterns (the `/RulesRegulations/VAT/Documents/` pattern that worked for the two bonus finds above) — none resolved. Listed here with their exact catalog URLs for manual follow-up (ZATCA site search, or contacting ZATCA directly):

- **Professional Services Guideline**: `https://zatca.gov.sa/en/HelpCenter/guidelines/Documents/VAT_Professional_Services_Guideline_English.pdf`
- **Agents Guideline**: `https://zatca.gov.sa/en/HelpCenter/guidelines/Documents/Agents%20Guideline.pdf`
- **Input Tax Deduction Guideline**: `https://zatca.gov.sa/en/HelpCenter/guidelines/Documents/Input%20Tax%20Deduction.pdf`
- **Real Estate Exemption Guideline**: `https://zatca.gov.sa/en/MediaCenter/Publications/Documents/Exemption%20of%20Real%20Estate%20Supplies%20-%20English.pdf`
- **Regional Headquarters Guideline**: `https://zatca.gov.sa/en/HelpCenter/guidelines/Documents/Guideline%20for%20Regional%20Headquarters%20in%20KSA%20(2).pdf`

## NOT gathered — confirmed pure navigation, nothing to ingest

Checked and confirmed these catalog entries have no substantive content of their own (pure links/menus/boilerplate) — deliberately not converted into documents, since there'd be nothing real to chunk:

- **Guidelines & Manuals index** (`.../HelpCenter/guidelines/Pages/default.aspx`) — also JS-rendered (SharePoint), so even the link list itself wasn't statically fetchable
- **Zakat, Customs and Tax Regulations broader index** (`.../RulesRegulations/Pages/systems.aspx`)
- **SOCPA Home** (`socpa.org.sa/socpa/home.aspx`)

## A methodology gotcha worth knowing

The first pass at converting page content used a tool that summarizes pages through a small AI model rather than extracting exact text — fine for figuring out *what's on* a page, but not something a compliance knowledge base should be built on (a paraphrased "Article 14 requires roughly..." is not the same as the actual legal text). **All four page-derived documents were subsequently redone** by fetching the raw HTML directly and parsing it with BeautifulSoup instead, to guarantee word-for-word accuracy — this caught at least one real omission (the E-Invoicing rollout phase dates weren't in the first, summarized pass at all).

## Formatting note for ingestion

The SOCPA regulations PDF uses `Article (N) :` (with parentheses) rather than the `Article N:` pattern `features/explainer/offline/chunker.py`'s regex currently expects. This will need either a chunker regex update or a source-text normalization pass before this specific file chunks correctly — flagging now so it's not a surprise at ingestion time.

## Language-tag mismatch found 2026-07-23

`gcc_unified_vat_agreement_ar.pdf` was gathered from the `/ar/` URL path and manifested as `language: "ar"`, but the PDF's actual content is the **English** translation of the agreement (headings read "Article (2)", "Article (3) Calculation of Dates", etc. — no meaningful Arabic text in it at all, confirmed via direct extraction). ZATCA appears to host this English version under the Arabic section of their site. `manifest.json` has been corrected to `language: "en"` so the English chunker actually runs on it. This does NOT mean a real Arabic version of the GCC agreement has been located — if one is needed, it's still ungathered; flagging rather than silently assuming this file covers the Arabic requirement.
