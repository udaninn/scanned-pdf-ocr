"""Turn a page image into words, and words back into a table.

Tesseract will happily give you the text of a scanned table. What it will not
give you is the table: the moment the words are joined into lines, the columns
are gone and an empty cell is indistinguishable from a narrow one. So we ask
for the word boxes instead and rebuild the grid from the geometry, which is
the only place the column structure still exists.
"""

from __future__ import annotations

import csv
import glob
import io
import shutil
import statistics
import subprocess
from dataclasses import dataclass, field

RENDER_TIMEOUT = 180
OCR_TIMEOUT = 300


class ToolMissing(RuntimeError):
    """A command-line tool this Actor depends on is not installed."""


def require_tools() -> None:
    for tool in ("pdftoppm", "tesseract"):
        if shutil.which(tool) is None:
            raise ToolMissing(
                "%s is not installed in the Actor image." % tool
            )


@dataclass
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def middle(self) -> float:
        return self.y + self.h / 2


@dataclass
class Table:
    header: list[str | None] | None
    rows: list[list[str | None]] = field(default_factory=list)
    mean_confidence: float = 0.0
    unreadable: list[list[int]] = field(default_factory=list)
    columns: list[float] = field(default_factory=list)
    unit: float = 0.0
    pages: list[int] = field(default_factory=list)

    @property
    def column_count(self) -> int:
        if self.header:
            return len(self.header)
        return max((len(r) for r in self.rows), default=0)

    @property
    def empty_cells(self) -> int:
        return sum(1 for r in self.rows for c in r if c is None)


def render_page(pdf_path: str, page: int, dpi: int, out_prefix: str) -> str:
    """Rasterise one page to greyscale PNG and return its path."""
    # A prefix of its own per page: pdftoppm pads the page number to the width
    # of the page count, so guessing the file name back is a losing game, and a
    # leftover image from an earlier page must never be mistaken for this one.
    prefix = "%s-p%d" % (out_prefix, page)
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-gray", "-png",
         "-f", str(page), "-l", str(page), pdf_path, prefix],
        check=True, capture_output=True, timeout=RENDER_TIMEOUT,
    )
    found = sorted(glob.glob(prefix + "*.png"))
    if not found:
        raise RuntimeError("page %d produced no image" % page)
    return found[0]


def read_words(image_path: str, languages: list[str]) -> list[Word]:
    """Run Tesseract and return every word box it found, confidence included.

    Nothing is filtered here on purpose. A smudge Tesseract could not read is
    still ink on the page, and if we drop it now the cell it sat in becomes
    indistinguishable from an empty one - which is the whole failure this
    Actor exists to avoid. Filtering happens later, where the position can be
    remembered even when the text cannot be trusted.
    """
    lang = "+".join(languages) if languages else "eng"
    proc = subprocess.run(
        ["tesseract", image_path, "stdout", "-l", lang, "--psm", "6",
         "-c", "preserve_interword_spaces=1", "tsv"],
        capture_output=True, text=True, check=True, timeout=OCR_TIMEOUT,
    )
    words: list[Word] = []
    reader = csv.DictReader(
        io.StringIO(proc.stdout), delimiter="\t", quoting=csv.QUOTE_NONE
    )
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            conf = float(row["conf"])
            if conf < 0:
                continue  # tesseract marks layout blocks with -1
            words.append(Word(text, int(row["left"]), int(row["top"]),
                              int(row["width"]), int(row["height"]), conf))
        except (KeyError, TypeError, ValueError):
            continue
    return words


def group_rows(words: list[Word]) -> list[list[Word]]:
    """Words sitting on the same baseline belong to the same row."""
    if not words:
        return []
    height = statistics.median(w.h for w in words)
    ordered = sorted(words, key=lambda w: w.middle)
    rows: list[list[Word]] = [[ordered[0]]]
    for word in ordered[1:]:
        if abs(word.middle - rows[-1][-1].middle) <= height * 0.7:
            rows[-1].append(word)
        else:
            rows.append([word])
    for row in rows:
        row.sort(key=lambda w: w.x)
    return rows


def split_cells(row: list[Word], gap: float,
                min_confidence: int) -> list[dict]:
    """Split one row wherever the gap is wider than a word space.

    A cell whose words are all below the confidence floor keeps its place in
    the grid and is reported as unreadable, not as empty.
    """
    groups: list[list[Word]] = [[row[0]]]
    for word in row[1:]:
        if word.x - groups[-1][-1].right > gap:
            groups.append([word])
        else:
            groups[-1].append(word)

    cells = []
    for group in groups:
        good = [w for w in group if w.conf >= min_confidence]
        cells.append({
            "text": " ".join(w.text for w in good) if good else None,
            "x": group[0].x,
            "conf": (sum(w.conf for w in good) / len(good)) if good else 0.0,
            "readable": bool(good),
        })
    return cells


def build_table(words: list[Word], min_rows: int, min_columns: int,
                min_confidence: int,
                min_filled: float = 0.35) -> Table | None:
    """Rebuild a grid from word boxes, keeping empty cells as None.

    Returns None when the page has no table on it. That judgement is the hard
    part: run the column finder over a page of prose and it will happily carve
    the sentences into a dozen ragged columns and call it a table. A real table
    is mostly full, so a grid that is mostly holes is thrown away rather than
    handed over as though it meant something.
    """
    rows = group_rows(words)
    if not rows:
        return None

    height = statistics.median(w.h for w in words)
    gap = height * 1.4
    row_cells = [split_cells(r, gap, min_confidence) for r in rows]

    # Only rows that look tabular get a say in where the columns are;
    # a title or a paragraph would otherwise drag a column out of line.
    anchors = sorted(
        c["x"] for cells in row_cells if len(cells) >= min_columns
        for c in cells
    )
    if not anchors:
        return None

    clusters: list[list[int]] = [[anchors[0]]]
    for x in anchors[1:]:
        if x - clusters[-1][-1] <= height * 2.5:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    columns = [statistics.median(c) for c in clusters]
    if len(columns) < min_columns:
        return None

    grid: list[list[str | None]] = []
    unreadable: list[tuple[int, int]] = []
    confidences: list[float] = []
    for cells in row_cells:
        if len(cells) < 2:
            continue  # a heading or a line of prose, not part of the table
        line: list[str | None] = [None] * len(columns)
        blind: set[int] = set()
        for cell in cells:
            index = min(range(len(columns)),
                        key=lambda i: abs(cell["x"] - columns[i]))
            if not cell["readable"]:
                blind.add(index)      # a set: two smudges in one cell is one cell
                continue
            line[index] = (cell["text"] if line[index] is None
                           else line[index] + " " + cell["text"])
            confidences.append(cell["conf"])
        # A cell only counts as unreadable if nothing legible landed there too.
        unreadable.extend((len(grid), i) for i in sorted(blind)
                          if line[i] is None)
        grid.append(line)

    if len(grid) < min_rows:
        return None

    filled = sum(1 for row in grid for cell in row if cell is not None)
    capacity = len(grid) * len(columns)
    marked = filled + len(unreadable)
    if capacity and marked / capacity < min_filled:
        return None
    # A grid where most of the ink defeated the OCR is not a table we can
    # honestly hand over; it is a bad scan, and saying so is more use than
    # a tidy-looking shell with the values missing.
    if marked and filled / marked < 0.5:
        return None

    header = grid[0] if grid else None
    body = grid[1:] if len(grid) > 1 else []
    offset = 1
    if not body:
        header, body, offset = None, grid, 0
    return Table(
        header=header,
        rows=body,
        mean_confidence=round(
            sum(confidences) / len(confidences), 1) if confidences else 0.0,
        unreadable=[[r - offset, c] for r, c in unreadable if r - offset >= 0],
        columns=list(columns),
        unit=height,
    )


def to_markdown(table: Table) -> str:
    def cell(value: str | None) -> str:
        return "" if value is None else value.replace("|", "\\|")

    width = table.column_count
    lines = []
    if table.header:
        lines.append("| " + " | ".join(cell(c) for c in table.header) + " |")
        lines.append("|" + "---|" * width)
    else:
        lines.append("|" + " |" * width)
        lines.append("|" + "---|" * width)
    for row in table.rows:
        padded = list(row) + [None] * (width - len(row))
        lines.append("| " + " | ".join(cell(c) for c in padded) + " |")
    return "\n".join(lines)


def to_csv(table: Table) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    if table.header:
        writer.writerow(["" if c is None else c for c in table.header])
    for row in table.rows:
        writer.writerow(["" if c is None else c for c in row])
    return buffer.getvalue().strip()


def _same_shape(a: Table, b: Table) -> bool:
    """Do two tables look like one table that a page break cut in half?"""
    if a.column_count != b.column_count or not a.columns or not b.columns:
        return False
    if len(a.columns) != len(b.columns):
        return False
    tolerance = max(a.unit, b.unit) * 2.0
    return all(abs(x - y) <= tolerance for x, y in zip(a.columns, b.columns))


def _looks_like(a: list[str | None] | None, b: list[str | None] | None) -> bool:
    """Same header, allowing for OCR wobble in case and spacing."""
    if a is None or b is None or len(a) != len(b):
        return False
    def norm(cell: str | None) -> str:
        return "" if cell is None else " ".join(cell.split()).lower()
    return [norm(c) for c in a] == [norm(c) for c in b]


def join_across_pages(pages: list[tuple[int, Table]]) -> list[Table]:
    """Stitch a table back together when it runs over a page break.

    Financial statements and long reports do this constantly, and a reader has
    no trouble with it: the columns are in the same places and the rows simply
    carry on. Two separate tables in the output, on the other hand, are two
    separate things to reconcile by hand.

    A continuation page usually has no header of its own, so whatever
    build_table took for a header is really the first data row - unless the
    document repeats the header on every page, in which case it is a duplicate
    and should go.
    """
    merged: list[Table] = []
    for number, table in pages:
        table.pages = table.pages or [number]
        previous = merged[-1] if merged else None
        contiguous = (
            previous is not None
            and previous.pages[-1] == number - 1
            and _same_shape(previous, table)
        )
        if not contiguous:
            merged.append(table)
            continue

        offset = len(previous.rows)
        if _looks_like(previous.header, table.header):
            pass                      # repeated header: drop it
        elif table.header is not None:
            previous.rows.append(table.header)
            offset += 1
        previous.rows.extend(table.rows)
        previous.unreadable.extend([r + offset, c] for r, c in table.unreadable)
        previous.pages.append(number)
        if table.mean_confidence:
            scores = [s for s in (previous.mean_confidence,
                                  table.mean_confidence) if s]
            previous.mean_confidence = round(sum(scores) / len(scores), 1)
    return merged
