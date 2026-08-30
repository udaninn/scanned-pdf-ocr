"""Read scanned PDFs: per-page text, and the tables inside them.

A scanned PDF is a stack of photographs. Nothing in it is text until something
reads the pixels, and most tools stop once they have produced a paragraph. That
is fine for prose and useless for a table, so this Actor keeps the coordinates
around long enough to put the columns back.
"""

from __future__ import annotations

import os
import tempfile

import httpx
import pdfplumber
from apify import Actor

from .ocr import (
    ToolMissing,
    build_table,
    read_words,
    render_page,
    require_tools,
    to_csv,
    to_markdown,
)

DOWNLOAD_TIMEOUT = 120
LANGUAGES = {
    "eng", "deu", "fra", "spa", "ita", "por", "nld", "kor", "jpn", "chi_sim",
}


def _short(exc: Exception, limit: int = 140) -> str:
    """One tidy line: library errors love to arrive as paragraphs."""
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _clean_languages(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return ["eng"]
    keep = [str(x).strip() for x in raw if str(x).strip() in LANGUAGES]
    return keep or ["eng"]


async def _download(url: str, max_bytes: int, path: str) -> int:
    """Stream a PDF to disk, stopping if it turns out to be too big."""
    size = 0
    async with httpx.AsyncClient(follow_redirects=True,
                                 timeout=DOWNLOAD_TIMEOUT) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with open(path, "wb") as handle:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("file is larger than the limit")
                    handle.write(chunk)
    return size


def _text_layer(pdf_path: str) -> tuple[int, list[str]]:
    """Return the page count and whatever text is already in the file."""
    with pdfplumber.open(pdf_path) as pdf:
        pages = [(page.extract_text() or "") for page in pdf.pages]
    return len(pages), pages


async def _emit_note(record_type: str, url: str, name: str,
                     title: str, hint: str) -> None:
    await Actor.push_data({
        "recordType": record_type,
        "sourceUrl": url,
        "fileName": name,
        "title": title,
        "hint": hint,
    })


async def _handle_pdf(url: str, opts: dict) -> int:
    """Process one PDF. Returns the number of records pushed."""
    name = url.rsplit("/", 1)[-1] or "document.pdf"
    pushed = 0

    with tempfile.TemporaryDirectory() as work:
        pdf_path = os.path.join(work, "input.pdf")
        try:
            await _download(url, opts["max_bytes"], pdf_path)
        except Exception as exc:  # noqa: BLE001 - one bad URL must not end the run
            await _emit_note(
                "error", url, name, "This PDF could not be downloaded",
                "%s: %s. Check that the link points straight at a PDF and is "
                "reachable without a login."
                % (type(exc).__name__, _short(exc)),
            )
            return 1

        try:
            page_count, existing = _text_layer(pdf_path)
        except Exception as exc:  # noqa: BLE001
            await _emit_note(
                "error", url, name, "This file could not be opened as a PDF",
                "%s. Password-protected and damaged files cannot be read."
                % type(exc).__name__,
            )
            return 1

        # Presence of a text layer is not the same as having one worth using:
        # plenty of scans carry a stamped header or a single OCR'd word and
        # nothing else. Judge it by density, or those files skip OCR and come
        # back nearly empty.
        chars = sum(len(t.strip()) for t in existing)
        has_text = chars >= 40 * max(page_count, 1)
        use_ocr = opts["force_ocr"] or not has_text

        limit = opts["max_pages"] or page_count
        pages = range(1, min(page_count, limit) + 1)

        for number in pages:
            if use_ocr:
                try:
                    image = render_page(pdf_path, number, opts["dpi"],
                                        os.path.join(work, "pg"))
                    words = read_words(image, opts["languages"])
                    os.unlink(image)
                except Exception as exc:  # noqa: BLE001
                    await _emit_note(
                        "error", url, name,
                        "Page %d could not be read" % number,
                        "%s. Try a lower DPI if the page is very large."
                        % type(exc).__name__,
                    )
                    pushed += 1
                    continue
                mode = "ocr"
                sure = [w for w in words if w.conf >= opts["min_confidence"]]
                page_text = " ".join(w.text for w in sure)
                confidence = (round(sum(w.conf for w in sure) / len(sure), 1)
                              if sure else 0.0)
                table = build_table(words, opts["min_rows"],
                                    opts["min_columns"],
                                    opts["min_confidence"],
                                    opts["min_filled"])
            else:
                mode = "text-layer"
                page_text = existing[number - 1]
                confidence = 100.0
                words = []
                table = None

            if opts["want_text"]:
                await Actor.push_data({
                    "recordType": "pageText",
                    "sourceUrl": url,
                    "fileName": name,
                    "pageNumber": number,
                    "pageCount": page_count,
                    "text": page_text,
                    "wordCount": len(page_text.split()),
                    "meanConfidence": confidence,
                    "extractionMode": mode,
                })
                pushed += 1

            if opts["want_tables"] and table is not None:
                record = {
                    "recordType": "table",
                    "sourceUrl": url,
                    "fileName": name,
                    "pageNumber": number,
                    "pageCount": page_count,
                    "header": table.header,
                    "rows": table.rows,
                    "rowCount": len(table.rows),
                    "columnCount": table.column_count,
                    "emptyCellCount": table.empty_cells - len(table.unreadable),
                    "unreadableCellCount": len(table.unreadable),
                    "unreadableCells": table.unreadable,
                    "meanConfidence": table.mean_confidence,
                    "extractionMode": mode,
                }
                if table.unreadable:
                    record["hint"] = (
                        "%d cell(s) contained marks OCR could not read. They "
                        "are null like empty cells, but listed in "
                        "'unreadableCells' as [row, column] so you can tell "
                        "the two apart. Raising the DPI or lowering "
                        "'minConfidence' often recovers them."
                        % len(table.unreadable)
                    )
                if opts["markdown"]:
                    record["markdown"] = to_markdown(table)
                if opts["csv"]:
                    record["csv"] = to_csv(table)
                await Actor.push_data(record)
                pushed += 1

        if opts["want_tables"] and not use_ocr and has_text:
            await _emit_note(
                "notice", url, name,
                "This PDF already contained text, so OCR was skipped",
                "Nothing was scanned: the page text above came straight out of "
                "the file, which is faster and free of OCR mistakes. For table "
                "structure in a PDF like this, PDF Table Extractor is the "
                "better tool. Set 'forceOcr' to true if the existing text "
                "layer is itself wrong.",
            )
            pushed += 1

    return pushed


async def main() -> None:
    async with Actor:
        try:
            require_tools()
        except ToolMissing as exc:
            Actor.log.error(str(exc))
            raise

        raw = await Actor.get_input() or {}
        urls = [str(u).strip() for u in (raw.get("pdfUrls") or [])
                if str(u).strip()]
        if not urls:
            await _emit_note(
                "error", "", "", "No PDF URLs were given",
                "Add at least one direct link to a PDF in the 'pdfUrls' field.",
            )
            return

        mode = raw.get("outputMode", "both")
        opts = {
            "languages": _clean_languages(raw.get("languages")),
            "dpi": max(150, min(600, int(raw.get("dpi", 300) or 300))),
            "min_confidence": max(0, min(100,
                                         int(raw.get("minConfidence", 40) or 0))),
            "min_rows": max(1, int(raw.get("minRows", 2) or 2)),
            "min_columns": max(2, int(raw.get("minColumns", 2) or 2)),
            "min_filled": max(0, min(100,
                                     int(raw.get("minFilledPercent", 35)
                                         if raw.get("minFilledPercent") is not None
                                         else 35))) / 100,
            "max_pages": max(0, int(raw.get("maxPagesPerPdf", 0) or 0)),
            "max_bytes": max(1, int(raw.get("maxFileSizeMb", 50) or 50)) * 1024 * 1024,
            "force_ocr": bool(raw.get("forceOcr", False)),
            "markdown": bool(raw.get("includeMarkdown", True)),
            "csv": bool(raw.get("includeCsv", True)),
            "want_text": mode in ("both", "text"),
            "want_tables": mode in ("both", "tables"),
        }

        Actor.log.info(
            "Reading %d PDF(s) at %d DPI in %s"
            % (len(urls), opts["dpi"], "+".join(opts["languages"]))
        )

        total = 0
        for url in urls:
            try:
                total += await _handle_pdf(url, opts)
            except Exception as exc:  # noqa: BLE001
                Actor.log.exception("unhandled failure on %s" % url)
                await _emit_note(
                    "error", url, url.rsplit("/", 1)[-1],
                    "This PDF could not be processed",
                    "%s. The other URLs in this run were unaffected."
                    % type(exc).__name__,
                )
                total += 1

        if total == 0:
            await _emit_note(
                "noResults", "", "", "Nothing was found to return",
                "The run finished without errors but produced no records. If "
                "you asked for tables only, the pages may not contain any that "
                "meet 'minRows' and 'minColumns'. Set 'outputMode' to 'both' "
                "to see the page text the OCR did read.",
            )
