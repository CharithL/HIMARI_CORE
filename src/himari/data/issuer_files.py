"""Issuer distribution histories for Q10, saved by hand from the issuers' own pages (v1.10).

Personal use, no scripts: the user saves each CAD-listed fund's distribution history into
`data/issuer/` (gitignored, so the files are never committed or shared), named by symbol with
the extension the download had:

- VCN, VUN, VIU, VEE, VGV (Vanguard): product page, "Prices and distribution", "Distribution
  history", Download;
- CGL.C (BlackRock): product page, the fund-data Download (.xls);
- DLR, CBIL (Global X): the product page saved whole (Save Page As, Webpage Complete): every
  year's distribution table is in the page.

PDBC is US-listed and outside Q10. The reader finds the distribution table in CSV, XLSX, Excel
2003 XML or HTML by its column headers, as the issuers' pages showed them on 15 November 2026.
Every cell is read at its own column: spreadsheets leave empty cells out and HTML may leave end
tags out, and neither may move an amount into the next column (that would turn a reinvested
distribution into cash). A file with other headers, an unreadable date or amount, or a binary
.xls is refused with what it holds, never guessed at. December rows are then compared with the
vendor's actions, so the person confirming Q10 sees the cash/reinvested split side by side.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree

if TYPE_CHECKING:
    from himari.core.types import CorporateAction

# the columns each issuer's distribution table shows (read from their pages, 15 November 2026)
COLUMNS = {
    "vanguard": {
        "ex": "Ex-dividend date",
        "pay": "Payment date",
        "cash": "Cash distribution per unit",
        "reinvested": "Reinvestment distribution per unit",
    },
    "blackrock": {
        "ex": "Ex-Date",
        "pay": "Payable Date",
        "cash": "Cash Amount",
        "reinvested": "Reinvested",
    },
    "globalx": {
        "ex": "Ex-Dividend Date",
        "pay": "Payment Date",
        "amount": "Payment Amount",
        "type": "Type",
    },
}
FUND_ISSUER = {
    "VCN": "vanguard", "VUN": "vanguard", "VIU": "vanguard", "VEE": "vanguard", "VGV": "vanguard",
    "CGL.C": "blackrock", "DLR": "globalx", "CBIL": "globalx",
}  # fmt: skip
DATE_FORMATS = (
    "%b %d %Y", "%b %d, %Y", "%B %d, %Y", "%B %d %Y", "%d-%b-%Y",
    "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",  # ISO, as Excel XML types dates
)  # fmt: skip
EMPTY = ("", "-", "–", "—")  # what the tables show for "none"
TOLERANCE = Decimal("0.00005")  # amounts are published to 5 decimals
MAX_XLSX_PART_BYTES = 50_000_000  # an uncompressed sheet larger than this is refused
SERIAL_DATES = (date(1990, 1, 1), date(2100, 1, 1))  # where an XLSX day number may land
SPREADSHEET_ML = "urn:schemas-microsoft-com:office:spreadsheet"
XLSX_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

Table = list[list[str]]


class IssuerFileError(ValueError):
    """A saved issuer file the reader will not interpret."""


@dataclass(frozen=True)
class IssuerDistribution:
    symbol: str
    ex_date: date
    pay_date: date | None
    cash: Decimal
    reinvested: Decimal
    source: str  # the file it came from


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def _date(text: str, where: str, serial_epoch: date | None = None) -> date | None:
    """A date as the issuer writes it; in an XLSX also the workbook's day number."""
    t = " ".join(text.split())
    if t in EMPTY:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    if serial_epoch is not None and re.fullmatch(r"\d{1,6}(\.0*)?", t):
        day = serial_epoch + timedelta(days=int(t.split(".")[0]))
        if SERIAL_DATES[0] <= day < SERIAL_DATES[1]:
            return day
    raise IssuerFileError(f"{where}: unreadable date {t!r}")


def _amount(text: str, where: str) -> Decimal:
    """A per-unit amount as the tables write it ("$0.38809", "0.11221 CAD"); a dash is zero.
    A comma is refused: per-unit amounts have no thousands, so it would be a decimal comma."""
    t = " ".join(text.split()).replace("$", "").replace("CAD", "").strip()
    if t in EMPTY:
        return Decimal(0)
    try:
        value = Decimal(t)
    except InvalidOperation:
        raise IssuerFileError(f"{where}: unreadable amount {text!r}") from None
    if not value.is_finite() or value < 0:
        raise IssuerFileError(f"{where}: unreadable amount {text!r}")
    return value


# ---- tables out of each format, every cell at its own column --------------------------------
@dataclass
class _OpenTable:
    rows: Table = field(default_factory=list)
    row: list[str] | None = None
    cell: list[str] | None = None

    def end_cell(self) -> None:
        if self.cell is not None:
            if self.row is None:
                self.row = []
            self.row.append(" ".join("".join(self.cell).split()))
        self.cell = None

    def end_row(self) -> None:
        self.end_cell()
        if self.row is not None:
            self.rows.append(self.row)
        self.row = None


class _HtmlTables(HTMLParser):
    """Each <table>'s rows of cell texts. An end tag the page leaves out (</td>, </tr>) is
    implied by the next cell or row, and a table inside a cell is a table of its own, so no
    text lands in another column, row or table."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[Table] = []
        self._open: list[_OpenTable] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._open.append(_OpenTable())
            self.tables.append(self._open[-1].rows)
        elif self._open and tag == "tr":
            self._open[-1].end_row()
            self._open[-1].row = []
        elif self._open and tag in ("td", "th"):
            self._open[-1].end_cell()
            self._open[-1].cell = []

    def handle_endtag(self, tag: str) -> None:
        if not self._open:
            return
        if tag in ("td", "th"):
            self._open[-1].end_cell()
        elif tag == "tr":
            self._open[-1].end_row()
        elif tag == "table":
            self._open.pop().end_row()

    def handle_data(self, data: str) -> None:
        if self._open and self._open[-1].cell is not None:
            self._open[-1].cell.append(data)


def _xml(raw: bytes) -> ElementTree.Element:
    """Parse XML that declares no DTD: entity expansion and external entities both need one,
    so a file with a DOCTYPE or ENTITY declaration is refused rather than parsed."""
    low = raw.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise IssuerFileError("an XML file with a DTD or entity declarations is refused")
    return ElementTree.fromstring(raw)


def _column(ref: str) -> int:
    """The 0-based column of an XLSX cell reference such as "C12" (-1 without one)."""
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - ord("A") + 1
    return n - 1


def _xlsx_tables(raw: bytes) -> tuple[list[Table], date]:
    """Each sheet's rows with every cell at the column its reference names (Excel leaves empty
    cells out), and the day the workbook's date numbers count from."""
    m = f"{{{XLSX_MAIN}}}"
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if any(i.file_size > MAX_XLSX_PART_BYTES for i in z.infolist()):
            raise IssuerFileError("an XLSX part is too large to be a distribution history")
        names = set(z.namelist())
        epoch = date(1899, 12, 30)  # the 1900 date system (exact from March 1900 on)
        if "xl/workbook.xml" in names:
            props = _xml(z.read("xl/workbook.xml")).find(f"{m}workbookPr")
            if props is not None and props.get("date1904", "").lower() in ("1", "true"):
                epoch = date(1904, 1, 1)
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = _xml(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in s.iter(f"{m}t")) for s in root.iter(f"{m}si")]
        tables = []
        for name in sorted(n for n in names if n.startswith("xl/worksheets/sheet")):
            rows = []
            for row in _xml(z.read(name)).iter(f"{m}row"):
                cells: list[str] = []
                for c in row.findall(f"{m}c"):
                    cells.extend([""] * (_column(c.get("r", "")) - len(cells)))
                    inline, value = c.find(f"{m}is"), c.find(f"{m}v")
                    if inline is not None:
                        text = "".join(t.text or "" for t in inline.iter(f"{m}t"))
                    else:
                        text = (value.text or "") if value is not None else ""
                    cells.append(shared[int(text)] if c.get("t") == "s" and text else text)
                rows.append(cells)
            tables.append(rows)
    return tables, epoch


def _spreadsheet_ml_tables(raw: bytes) -> list[Table]:
    """Each worksheet's rows. A cell may name its column (ss:Index, skipping empty ones) or
    span several (ss:MergeAcross); an empty <Cell/> still takes its column."""
    ss = f"{{{SPREADSHEET_ML}}}"
    tables = []
    for sheet in _xml(raw).iter(f"{ss}Worksheet"):
        rows = []
        for row in sheet.iter(f"{ss}Row"):
            cells: list[str] = []
            for cell in row.findall(f"{ss}Cell"):
                index = cell.get(f"{ss}Index")  # 1-based
                if index is not None:
                    cells.extend([""] * (int(index) - 1 - len(cells)))
                data = cell.find(f"{ss}Data")
                cells.append("".join(data.itertext()) if data is not None else "")
                cells.extend([""] * int(cell.get(f"{ss}MergeAcross", "0")))
            rows.append(cells)
        tables.append(rows)
    return tables


def _tables(path: Path) -> tuple[list[Table], date | None]:
    """The file's tables of cell texts, and for an XLSX the day its date numbers count from."""
    raw = path.read_bytes()
    if raw.startswith(b"\xd0\xcf\x11\xe0"):  # an OLE2 (binary .xls) file
        raise IssuerFileError(
            f"{path.name}: a binary .xls; open it in Excel and save it as .xlsx or .csv"
        )
    text = raw.decode("utf-8-sig", errors="replace")
    suffix = path.suffix.lower()
    try:
        if raw.startswith(b"PK"):
            return _xlsx_tables(raw)
        if SPREADSHEET_ML in text[:2000]:
            return _spreadsheet_ml_tables(raw), None
        if suffix in (".html", ".htm") or "<table" in text.lower():
            parser = _HtmlTables()
            parser.feed(text)
            parser.close()
            return parser.tables, None
        if suffix in (".csv", ".txt"):
            return [list(csv.reader(io.StringIO(text)))], None
    except IssuerFileError:
        raise
    except (
        zipfile.BadZipFile, ElementTree.ParseError, csv.Error, NotImplementedError, IndexError,
        ValueError,
    ) as exc:  # fmt: skip
        raise IssuerFileError(f"{path.name}: unreadable file ({exc})") from exc
    raise IssuerFileError(f"{path.name}: unknown format (expected CSV, XLSX, XML or HTML)")


# ---- reading one fund's file ----------------------------------------------------------------
def read_issuer_file(path: Path, symbol: str) -> list[IssuerDistribution]:
    """The fund's distributions, one per ex-date (cash and reinvested parts summed)."""
    issuer = FUND_ISSUER[symbol]
    cols = COLUMNS[issuer]
    want = {_norm(v) for v in cols.values()}
    tables, epoch = _tables(path)
    seen: Table = []  # rows before any header, shown when no table is found
    totals: dict[date, tuple[Decimal, Decimal, date | None]] = {}
    for table in tables:
        header: dict[str, int] | None = None
        for i, row in enumerate(table):
            names = [_norm(c) for c in row]
            if want <= set(names):
                header = {k: names.index(_norm(v)) for k, v in cols.items()}
                continue
            if header is None:
                if any(row):
                    seen.append(row)
                continue
            if not any(c.strip() for c in row):
                continue
            cells = row + [""] * (max(header.values()) + 1 - len(row))  # empty last cells
            where = f"{path.name} row {i + 1}"
            ex = _date(cells[header["ex"]], where, epoch)
            if ex is None:
                continue
            pay = _date(cells[header["pay"]], where, epoch)
            if issuer == "globalx":
                amount, kind = _amount(cells[header["amount"]], where), _norm(cells[header["type"]])
                if kind == "cash":
                    cash, reinvested = amount, Decimal(0)
                elif kind.startswith("reinvest"):
                    cash, reinvested = Decimal(0), amount
                else:
                    raise IssuerFileError(f"{where}: unknown distribution type {kind!r}")
            else:
                cash = _amount(cells[header["cash"]], where)
                reinvested = _amount(cells[header["reinvested"]], where)
            c0, r0, p0 = totals.get(ex, (Decimal(0), Decimal(0), None))
            totals[ex] = (c0 + cash, r0 + reinvested, p0 or pay)
    if not totals:
        found = "; ".join(" | ".join(r) for r in seen[:5]) or "none"
        raise IssuerFileError(
            f"{path.name}: no table with the columns {sorted(cols.values())}; first rows found: "
            f"{found}"
        )
    return [
        IssuerDistribution(symbol, ex, pay, cash, reinvested, path.name)
        for ex, (cash, reinvested, pay) in sorted(totals.items())
    ]


def find_issuer_files(folder: Path) -> dict[str, Path]:
    """Symbol -> its saved file; a symbol with more than one file is refused."""
    found: dict[str, Path] = {}
    for path in sorted(folder.iterdir()) if folder.exists() else []:
        sym = path.name[: -len(path.suffix)] if path.suffix else path.name
        if path.is_file() and sym in FUND_ISSUER:
            if sym in found:
                raise IssuerFileError(f"two files for {sym}: {found[sym].name} and {path.name}")
            found[sym] = path
    return found


# ---- December rows side by side with the vendor ---------------------------------------------
@dataclass(frozen=True)
class DecemberCheck:
    symbol: str
    ex_date: date
    issuer_cash: Decimal | None
    issuer_reinvested: Decimal | None
    vendor_cash: Decimal  # the vendor's cash distributions with this ex-date
    verdict: str  # match | reinvested_as_cash | vendor_missing | issuer_missing | differs


def compare_december(
    issuer: Sequence[IssuerDistribution], vendor: Sequence[CorporateAction], symbol: str
) -> list[DecemberCheck]:
    """Each December ex-date in either source, with a verdict. `match` means the vendor holds
    exactly the issuer's cash part (a reinvested part pays nothing, so the vendor should not
    list it as cash)."""
    iss = {d.ex_date: d for d in issuer if d.ex_date.month == 12}
    ven: dict[date, Decimal] = {}
    for a in vendor:
        if a.action_type == "cash" and a.ex_date.month == 12:
            ven[a.ex_date] = ven.get(a.ex_date, Decimal(0)) + a.amount
    out = []
    for ex in sorted(set(iss) | set(ven)):
        i, v = iss.get(ex), ven.get(ex, Decimal(0))
        if i is None:
            verdict = "issuer_missing"
        elif abs(v - i.cash) <= TOLERANCE and (v > 0 or i.cash == 0):
            verdict = "match"
        elif i.reinvested > 0 and abs(v - (i.cash + i.reinvested)) <= TOLERANCE:
            verdict = "reinvested_as_cash"
        elif v == 0 and i.cash > 0:
            verdict = "vendor_missing"
        else:
            verdict = "differs"
        out.append(DecemberCheck(symbol, ex, i.cash if i else None,
                                 i.reinvested if i else None, v, verdict))  # fmt: skip
    return out
