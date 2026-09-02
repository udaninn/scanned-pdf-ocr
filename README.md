# Scanned PDF OCR — text *and* tables, with the columns still attached

Read scanned or photographed PDFs with OCR and get two things back: clean text
for every page, and the actual tables that were on those pages, as rows and
columns you can put straight into a spreadsheet.

Built for scanned invoices, bank and brokerage statements, government filings,
old annual reports, tariff schedules, and any document that reached you as a
picture of a page rather than a file with text in it.

## The problem this solves

Every OCR tool will give you the words on a scanned page. Almost none will give
you the table, because the moment words are joined into lines the column
structure is gone.

Take a scanned page with this on it:

```
Item          Jan     Feb     Mar
Widgets        12             31
Gadgets                44     51
Doohickeys      7       9
```

A normal OCR pass returns:

```
Item Jan Feb Mar Widgets 12 31 Gadgets 44 51 Doohickeys 7 9
```

Is `31` a February figure or a March one? There is no way to tell any more. The
blank cells left no trace, so every value after the first gap has silently
shifted one column to the left.

This Actor keeps the coordinates of every word and rebuilds the grid from the
geometry, which is the only place that structure still exists:

```json
{
  "header": ["Item", "Jan", "Feb", "Mar"],
  "rows": [
    ["Widgets", "12", null, "31"],
    ["Gadgets", null, "44", "51"],
    ["Doohickeys", "7", "9", null]
  ]
}
```

## "Empty" and "unreadable" are not the same thing

This is the part other OCR tools get wrong, and it matters most on exactly the
documents people scan.

When OCR meets a smudged digit it often returns a garbage character with a
confidence of zero. Drop that word — as any sane confidence filter does — and
the cell it occupied becomes empty. Your table now says a fee was never charged,
when in truth the number was simply blurry.

So low-confidence marks are never silently discarded here. The cell is still
`null`, but its position is reported separately:

```json
{
  "rows": [["Doohickeys", null, "9", null]],
  "emptyCellCount": 1,
  "unreadableCellCount": 1,
  "unreadableCells": [[0, 1]]
}
```

`[0, 1]` means row 0, column 1 held something the OCR could not read. The other
`null` in that row is genuinely blank. Raising `dpi` or lowering
`minConfidence` usually recovers these.


### Prose is not a table

Run a column finder over a page of paragraphs and it will cheerfully carve the
sentences into a dozen ragged columns. So a grid that comes out mostly empty is
discarded rather than returned: below `minFilledPercent` filled cells, the page
is reported as having no table instead of being handed a convincing-looking one
that means nothing. Lower the threshold for genuinely sparse forms.

## Tables that run over a page break

Financial statements and long reports split tables across pages constantly. A
reader has no trouble with it - the columns are in the same places and the rows
simply carry on - but two separate records are two things to reconcile by hand.

So a table whose columns line up with the one on the page before is joined onto
it, and the record carries a `pageSpan` saying where it came from:

```json
{ "pageNumber": 4, "pageSpan": [4, 5, 6], "rowCount": 71 }
```

A continuation page usually has no header of its own, so its first line is
treated as data. When the document repeats the header on every page instead,
the repeat is dropped rather than landing in the middle of your rows.

Set `joinAcrossPages` to false to keep one record per page.

## What you get

- **Per-page text** — everything OCR read, with a mean confidence score
- **Tables as real grids** — header row, data rows, empty cells as `null`
- **Unreadable cells flagged**, never disguised as empty
- **Markdown and CSV** in every table record — paste into a sheet or an LLM prompt
- **10 languages** — English, German, French, Spanish, Italian, Portuguese,
  Dutch, Korean, Japanese, Simplified Chinese. Combine them for mixed documents
- **Digital PDFs are not OCR'd** — if the file already has a text layer it is
  read directly, which is faster, exact, and cheaper for you
- **No proxy, no API key, no cloud OCR account.** Tesseract runs inside the Actor

## Input

```json
{
  "pdfUrls": ["https://example.com/scanned-statement.pdf"],
  "languages": ["eng"],
  "outputMode": "both",
  "dpi": 300,
  "minConfidence": 40,
  "minRows": 2,
  "minColumns": 2,
  "maxPagesPerPdf": 3
}
```

| Field | Default | Notes |
|---|---|---|
| `pdfUrls` | — | Required. Direct links to the PDFs. |
| `languages` | `["eng"]` | Tesseract codes, most likely first: `eng`, `deu`, `fra`, `spa`, `ita`, `por`, `nld`, `kor`, `jpn`, `chi_sim`. |
| `outputMode` | `both` | `both`, `tables` or `text`. |
| `dpi` | `300` | Higher is more accurate and slower. 400 for small print, 200 for clean large type. |
| `minConfidence` | `40` | 0–100. Below this a word is treated as unreadable rather than as text. |
| `joinAcrossPages` | `true` | Rejoin a table that a page break cut in half, and drop a header the document repeats on every page. |
| `minRows` / `minColumns` | `2` / `2` | Keep `minColumns` at 2+ so paragraphs are not mistaken for tables. |
| `minFilledPercent` | `35` | A real table is mostly full. Grids emptier than this are discarded as false detections. |
| `maxPagesPerPdf` | `3` | 0 reads every page. OCR is charged per page, so this stops at 3 by default rather than running up a bill on a long document - and it says so in the output whenever it had to stop early. |
| `maxFileSizeMb` | `50` | Larger files are skipped with an explanatory record. |
| `forceOcr` | `false` | Run OCR even when a text layer exists. |
| `includeMarkdown` / `includeCsv` | `true` | Extra formats on each table record. |

## Output

Three kinds of record, told apart by `recordType`.

**`table`** — one per detected table:

| Field | Description |
|---|---|
| `sourceUrl`, `fileName`, `pageNumber`, `pageCount` | Where it came from |
| `header` | Header row, or `null` if none was detected |
| `rows` | Data rows. Empty cells are `null`. |
| `rowCount`, `columnCount` | Shape |
| `emptyCellCount` | Cells that were genuinely blank |
| `unreadableCellCount`, `unreadableCells` | Cells that held unreadable marks, as `[row, column]` |
| `meanConfidence` | 0–100 across the cells that were read |
| `extractionMode` | `ocr` or `text-layer` |
| `markdown`, `csv` | Ready-to-paste versions |

**`pageText`** — one per page: `text`, `wordCount`, `meanConfidence`,
`extractionMode`.

**`notice`, `error`, `noResults`** — plain-language records explaining what
happened. A run that finds nothing tells you why instead of handing back an
empty dataset.

Export as Excel, CSV, JSON or XML from the Apify Console, or pull it through
the API.

## Typical uses

- **Scanned invoices and statements** — line items into a spreadsheet
- **Government and regulatory filings** — the paper-era back catalogue
- **Old annual reports** — financial tables from PDFs that predate text layers
- **Research archives** — result tables from scanned journal articles
- **RAG and LLM pipelines** — feed a model a real table instead of a scrambled line

## Limits, stated plainly

- **OCR is a guess.** Clean 300 DPI print reads at high confidence; faint
  photocopies, dot-matrix print and handwriting do not. Use `meanConfidence` and
  `unreadableCells` to decide how far to trust a result.
- Rotated and vertically-written tables are not supported yet.
- Cells merged across rows are reported in the first row they occupy.
- Joining is by column geometry, so two unrelated tables that happen to share a
  column layout on consecutive pages will be joined. Check `pageSpan` if that
  matters, or turn joining off.
- Password-protected files are skipped with an `error` record.
- Photographs of pages taken at an angle read poorly; scan flat where you can.

## Running long documents

OCR is memory-hungry: a 300 DPI page is tens of megabytes before Tesseract even
looks at it. This Actor is set to 2 GB, which reads a page in roughly fifteen
seconds. At 1 GB the same page takes minutes, because the container spends its
time swapping rather than reading - so if you fork this Actor, keep the memory
up rather than turning the DPI down.

## Support

Found a PDF that reads badly? Open an issue on the Actor's **Issues** tab with
the URL and the page number. That is the main way the detector improves.

---

## Run it

- **Actor on Apify Store** — [scanned-pdf-ocr](https://apify.com/practical_ophthalmologist_iuq/scanned-pdf-ocr)
- **Call it from Python** — [/api/python](https://apify.com/practical_ophthalmologist_iuq/scanned-pdf-ocr/api/python)
- **Call it from JavaScript** — [/api/javascript](https://apify.com/practical_ophthalmologist_iuq/scanned-pdf-ocr/api/javascript)

## Related Actors

If your PDF already has a text layer, you do not need OCR at all:

- [pdf-table-extractor](https://apify.com/practical_ophthalmologist_iuq/pdf-table-extractor) — tables out of born-digital PDFs, same treatment of empty cells ([Python](https://apify.com/practical_ophthalmologist_iuq/pdf-table-extractor/api/python) | [JavaScript](https://apify.com/practical_ophthalmologist_iuq/pdf-table-extractor/api/javascript))

Same approach applied to job boards — read the official public API, keep the
structure, no proxy and no API key:

- [career-page-job-monitor](https://apify.com/practical_ophthalmologist_iuq/career-page-job-monitor) — Greenhouse, Lever, Ashby, Workable, Recruitee and SmartRecruiters in one run
- [workday-jobs-scraper](https://apify.com/practical_ophthalmologist_iuq/workday-jobs-scraper) — Workday careers sites, paste any careers URL
- [greenhouse-jobs-scraper](https://apify.com/practical_ophthalmologist_iuq/greenhouse-jobs-scraper) · [lever-jobs-scraper](https://apify.com/practical_ophthalmologist_iuq/lever-jobs-scraper) · [ashby-jobs-scraper](https://apify.com/practical_ophthalmologist_iuq/ashby-jobs-scraper) · [workable-jobs-scraper](https://apify.com/practical_ophthalmologist_iuq/workable-jobs-scraper) · [recruitee-jobs-scraper](https://apify.com/practical_ophthalmologist_iuq/recruitee-jobs-scraper) · [smartrecruiters-jobs-scraper](https://apify.com/practical_ophthalmologist_iuq/smartrecruiters-jobs-scraper)
