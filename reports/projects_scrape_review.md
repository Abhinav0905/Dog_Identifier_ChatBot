# DAR Projects Scrape Review

Generated: 2026-06-05

## Scope

The crawl starts at `https://dharamsalaanimalrescue.org/projects/` and follows
links found inside the page content for three levels. Global navigation,
donation, newsletter, shop, author, category, and account-style pages are
excluded so the project knowledge remains focused.

## Coverage

- Pages visited: 14
- Pages saved: 14
- Fetch failures: 0
- Low-content skips: 0
- Extracted project words: 5,939

Core pages:

- What we do
- ABC/AR - Animal Birth Control-Anti Rabies
- Dog Population Management in Dharamsala
- Street Animal Rescue
- Humane Education
- Rabies education
- Adopt a Desi Dog

Relevant child pages:

- Karuna Rural Village Program
- Street Animal Feeding
- Rabies Knowledge Quiz
- World Rabies Day Vaccination Camp
- How Communities Help Make Dharamsala Rabies-Free
- Adopt Molly
- Adopt Rosa

The full URL-by-URL crawl result is in `reports/projects_scrape_manifest.json`.

## Vectorization

- Chroma vectors: 156
- Unique documents: 44
- HNSW space: cosine
- HNSW M: 16
- HNSW construction ef: 200
- HNSW search ef: 100
- Website project pages present in Chroma: 14/14
- Supplied PDFs present in Chroma: 7/7

The four scanned PDFs were OCR-processed before vectorization. Two decorative
pages in `want-friend-be-friend-english.pdf` contained no extractable text.

## Retrieval Checks

The rebuilt collection returned the intended primary source for:

- ABC/AR program
- Karuna Rural Village Program
- Desi dog adoption
- Injured street animal rescue
- Dog-bite first aid
- Animal Birth Control Rules 2023
- Dharamsala Animal Rescue contact details

## Repeat The Refresh

```bash
python3 scripts/scrape_dar_site.py --scope projects --delay 10
python3 scripts/ingest_docs.py --chroma --clear-chroma --ocr-pdfs \
  --doc "/path/to/each/source.pdf"
```
