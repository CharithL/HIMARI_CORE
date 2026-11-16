"""Issuer distribution files saved by hand (Q10, v1.10): read in every supported format with
each cell in its own column, refused when they are not what the issuers' pages showed, and
compared with the vendor's December rows."""

import io
import json
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from himari.cli import app
from himari.core.types import CorporateAction
from himari.data.issuer_files import (
    IssuerFileError,
    compare_december,
    find_issuer_files,
    read_issuer_file,
)
from himari.data.storage import ACTIONS, VersionedStore, action_row

D = Decimal
VANGUARD_CSV = """Type,Ex-dividend date,Record date,Payment date,Cash distribution per unit,\
Reinvestment distribution per unit,Total distribution per unit
Income,Sep 18 2026,Sep 18 2026,Sep 25 2026,$0.37759,—,$0.37759
CGCA,Dec 30 2025,Dec 30 2025,Jan 07 2026,—,$0.34994,$0.34994
Income,Dec 30 2025,Dec 30 2025,Jan 07 2026,$0.38809,—,$0.38809
"""
VANGUARD_HEADER = VANGUARD_CSV.splitlines()[0].split(",")
GLOBALX_HTML = """<html><body><select><option>2025</option><option>2019</option></select>
<table><tr><th>Ex-Dividend Date</th><th>Record Date</th><th>Payment Date</th>
<th>Payment Amount</th><th>Type</th><th>Distribution Period</th></tr>
<tr><td>December 30, 2025</td><td>December 30, 2025</td><td>January 7, 2026</td>
<td>0.11221</td><td>Cash</td><td>Quarterly</td></tr>
<tr><td>December 30, 2025</td><td>December 30, 2025</td><td>January 7, 2026</td>
<td>0.05000</td><td>Reinvested</td><td>Annual</td></tr></table>
<table style="display:none"><tr><th>Ex-Dividend Date</th><th>Record Date</th>
<th>Payment Date</th><th>Payment Amount</th><th>Type</th><th>Distribution Period</th></tr>
<tr><td>December 27, 2019</td><td>December 30, 2019</td><td>January 7, 2020</td>
<td>0.02000</td><td>Cash</td><td>Quarterly</td></tr></table></body></html>
"""
BLACKROCK_HEADER = (
    "Ex-Date", "Payable Date", "Record Date", "Cash Amount", "Total Distribution", "Reinvested",
)  # fmt: skip


def row(*values: str) -> str:
    """An Excel XML row of text cells; '' is an empty <Cell/>."""
    cells = (f'<Cell><Data ss:Type="String">{v}</Data></Cell>' if v else "<Cell/>" for v in values)
    return "<Row>" + "".join(cells) + "</Row>"


def excel_xml(*rows: str) -> str:
    """BlackRock's fund-data .xls is Excel 2003 XML, with the distributions on their own sheet."""
    ns = "urn:schemas-microsoft-com:office:spreadsheet"
    return (
        f'<?xml version="1.0"?>\n<Workbook xmlns="{ns}" xmlns:ss="{ns}">'
        f'<Worksheet ss:Name="Holdings"><Table>{row("Ticker")}</Table></Worksheet>'
        f'<Worksheet ss:Name="Distributions"><Table>{row(*BLACKROCK_HEADER)}{"".join(rows)}'
        "</Table></Worksheet></Workbook>"
    )


def xlsx(rows: list[list[str | int]], date1904: bool = False) -> bytes:
    """A minimal XLSX laid out as Excel writes one: text in shared strings, numbers (dates
    among them) as numbers, each cell with its reference, and empty cells left out."""
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    strings = sorted({v for r in rows for v in r if isinstance(v, str) and v})

    def cell(ref: str, v: str | int) -> str:
        if isinstance(v, int):
            return f'<c r="{ref}"><v>{v}</v></c>'
        return f'<c r="{ref}" t="s"><v>{strings.index(v)}</v></c>'

    body = ""
    for i, r in enumerate(rows, start=1):
        cells = (cell(f"{chr(65 + j)}{i}", v) for j, v in enumerate(r) if v != "")
        body += "<row>" + "".join(cells) + "</row>"
    pr = f'<workbookPr date1904="{int(date1904)}"/>'
    sst = "".join(f"<si><t>{s}</t></si>" for s in strings)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f"<workbook {ns}>{pr}</workbook>")
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>{sst}</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{body}</sheetData>"
                   "</worksheet>")  # fmt: skip
    return buf.getvalue()


def write(tmp: Path, name: str, content: str | bytes) -> Path:
    path = tmp / name
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return path


def test_vanguard_csv_sums_the_cash_and_reinvested_rows(tmp_path: Path) -> None:
    rows = read_issuer_file(write(tmp_path, "VCN.csv", VANGUARD_CSV), "VCN")
    dec = next(r for r in rows if r.ex_date == date(2025, 12, 30))
    assert (dec.cash, dec.reinvested, dec.pay_date) == (
        D("0.38809"), D("0.34994"), date(2026, 1, 7),
    )  # fmt: skip
    assert [r.ex_date for r in rows] == [date(2025, 12, 30), date(2026, 9, 18)]


def test_globalx_page_reads_every_year_including_hidden_tables(tmp_path: Path) -> None:
    rows = read_issuer_file(write(tmp_path, "DLR.html", GLOBALX_HTML), "DLR")
    assert [(r.ex_date, r.cash, r.reinvested) for r in rows] == [
        (date(2019, 12, 27), D("0.02000"), D(0)),
        (date(2025, 12, 30), D("0.11221"), D("0.05000")),
    ]


def test_html_rows_survive_left_out_end_tags_and_nested_tables(tmp_path: Path) -> None:
    """HTML may leave </td> and </tr> out, and a table inside a cell is a table of its own:
    each value stays in its column and each row in its table."""
    page = (
        "<table><tr><th>Ex-Dividend Date<th>Payment Date<th>Payment Amount<th>Type"
        "<tr><td>December 30, 2025<td>January 7, 2026<td>0.11221"
        "<td><table><tr><td>see note</table>Cash"
        "<tr><td>December 30, 2025<td>January 7, 2026<td>0.05000<td>Reinvested</table>"
    )
    (r,) = read_issuer_file(write(tmp_path, "CBIL.html", page), "CBIL")
    assert (r.ex_date, r.pay_date, r.cash, r.reinvested) == (
        date(2025, 12, 30), date(2026, 1, 7), D("0.11221"), D("0.05000"),
    )  # fmt: skip


def test_blackrock_excel_2003_xml_finds_the_distributions_sheet(tmp_path: Path) -> None:
    page = excel_xml(row("Dec 22, 2025", "-", "Dec 23, 2025", "0.00", "2.10", "2.10"))
    (r,) = read_issuer_file(write(tmp_path, "CGL.C.xls", page), "CGL.C")
    assert (r.ex_date, r.pay_date, r.cash, r.reinvested) == (
        date(2025, 12, 22), None, D("0.00"), D("2.10"),
    )  # fmt: skip


def test_excel_xml_cells_stay_in_their_columns(tmp_path: Path) -> None:
    """An empty <Cell/> and a cell that names its column (ss:Index) leave the later cells in
    place; typed dates come in ISO form."""
    typed = (
        '<Row><Cell><Data ss:Type="DateTime">2025-12-22T00:00:00.000</Data></Cell><Cell/>'
        '<Cell ss:Index="4"><Data ss:Type="Number">0.1</Data></Cell>'
        '<Cell><Data ss:Type="Number">2.2</Data></Cell>'
        '<Cell><Data ss:Type="Number">2.1</Data></Cell></Row>'
    )
    (r,) = read_issuer_file(write(tmp_path, "CGL.C.xls", excel_xml(typed)), "CGL.C")
    assert (r.ex_date, r.pay_date, r.cash, r.reinvested) == (
        date(2025, 12, 22), None, D("0.1"), D("2.1"),
    )  # fmt: skip


def test_xlsx_is_read(tmp_path: Path) -> None:
    lines: list[list[str | int]] = [[*r.split(",")] for r in VANGUARD_CSV.strip().splitlines()]
    rows = read_issuer_file(write(tmp_path, "VUN.xlsx", xlsx(lines)), "VUN")
    assert rows[0].reinvested == D("0.34994")


@pytest.mark.parametrize(("date1904", "days_before"), [(False, 0), (True, 1462)])
def test_xlsx_cells_stay_in_their_columns(tmp_path: Path, date1904: bool, days_before: int) -> None:
    """Excel leaves empty cells out and may store dates as day numbers (counted from
    1899-12-30, or 1904-01-01 in a 1904 workbook). The CGCA row's empty cash cell must not let
    its reinvested amount slide into the cash column."""
    dec30, jan7 = 46021 - days_before, 46029 - days_before  # 2025-12-30 and 2026-01-07
    rows: list[list[str | int]] = [
        [*VANGUARD_HEADER],
        ["CGCA", dec30, dec30, jan7, "", "$0.34994", "$0.34994"],
        ["Income", dec30, dec30, jan7, "$0.38809"],  # Excel leaves the empty last cells out
    ]
    (r,) = read_issuer_file(write(tmp_path, "VUN.xlsx", xlsx(rows, date1904)), "VUN")
    assert (r.ex_date, r.pay_date, r.cash, r.reinvested) == (
        date(2025, 12, 30), date(2026, 1, 7), D("0.38809"), D("0.34994"),
    )  # fmt: skip


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("VCN.xls", b"\xd0\xcf\x11\xe0binary", "binary .xls"),
        ("VCN.xlsx", b"PK\x03\x04 cut short", "unreadable file"),
        ("VCN.csv", "Date,Amount\n2025-12-30,0.4\n", "no table with the columns"),
        ("VCN.csv", VANGUARD_CSV.replace("Dec 30 2025", "12/30/2025"), "unreadable date"),
        ("DLR.html", GLOBALX_HTML.replace("0.11221", "0,11221"), "unreadable amount"),
        ("DLR.html", GLOBALX_HTML.replace("Reinvested", "Special"), "unknown distribution type"),
        ("CGL.C.xls", excel_xml().replace("?>", '?><!DOCTYPE x [<!ENTITY a "b">]>', 1), "DTD"),
    ],
)
def test_files_that_are_not_what_the_pages_showed_are_refused(
    tmp_path: Path, name: str, content: str | bytes, message: str
) -> None:
    symbol = name.rsplit(".", 1)[0]
    with pytest.raises(IssuerFileError, match=message):
        read_issuer_file(write(tmp_path, name, content), symbol)


def test_files_are_found_by_symbol(tmp_path: Path) -> None:
    write(tmp_path, "CGL.C.xls", excel_xml())
    write(tmp_path, "VCN.csv", VANGUARD_CSV)
    write(tmp_path, "notes.txt", "ignored")
    (tmp_path / "DLR_files").mkdir()  # what Save Page As writes beside the page
    assert {s: p.name for s, p in find_issuer_files(tmp_path).items()} == {
        "CGL.C": "CGL.C.xls", "VCN": "VCN.csv",
    }  # fmt: skip
    write(tmp_path, "VCN.xlsx", b"PK")
    with pytest.raises(IssuerFileError, match="two files for VCN"):
        find_issuer_files(tmp_path)


def cash(day: date, amount: str) -> CorporateAction:
    return CorporateAction(symbol="VCN", ex_date=day, action_type="cash", amount=D(amount))


def test_december_verdicts(tmp_path: Path) -> None:
    issuer = read_issuer_file(write(tmp_path, "VCN.csv", VANGUARD_CSV), "VCN")
    dec = date(2025, 12, 30)

    def verdict(vendor: list[CorporateAction]) -> list[str]:
        return [c.verdict for c in compare_december(issuer, vendor, "VCN")]

    assert verdict([cash(dec, "0.38809")]) == ["match"]
    assert verdict([cash(dec, "0.388090")]) == ["match"]  # the same number
    assert verdict([cash(dec, "0.38810")]) == ["differs"]  # v1.11: a match is exact
    assert verdict([cash(dec, "0.73803")]) == ["reinvested_as_cash"]  # 0.38809 + 0.34994
    assert verdict([]) == ["vendor_missing"]
    assert verdict([cash(dec, "0.5")]) == ["differs"]
    assert verdict([cash(dec, "0.38809"), cash(date(2024, 12, 30), "0.39")]) == [
        "issuer_missing", "match",
    ]  # fmt: skip


def test_issuer_check_lists_missing_files_and_rows(tmp_path: Path) -> None:
    folder = tmp_path / "issuer"
    folder.mkdir()
    write(folder, "VCN.csv", VANGUARD_CSV)
    result = CliRunner().invoke(
        app, ["data", "issuer-check", "--folder", str(folder), "--data-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "VCN 2025-12-30: issuer cash 0.38809, reinvested 0.34994; vendor cash 0" in result.output
    assert "vendor_missing" in result.output and "VUN: no file" in result.output


def test_issuer_check_refuses_two_files_for_one_fund(tmp_path: Path) -> None:
    write(tmp_path, "VCN.csv", VANGUARD_CSV)
    write(tmp_path, "VCN.html", GLOBALX_HTML)
    result = CliRunner().invoke(
        app, ["data", "issuer-check", "--folder", str(tmp_path), "--data-dir", str(tmp_path)]
    )
    assert isinstance(result.exception, SystemExit) and result.exit_code == 1
    assert "two files for VCN: VCN.csv and VCN.html" in result.output


def eodhd_cash(data_dir: Path, *rows: tuple[date, str]) -> None:
    """EODHD cash distributions for VCN, in the store `issuer-check` reads."""
    t = datetime(2026, 1, 1, tzinfo=UTC)
    actions = [CorporateAction(symbol="VCN", ex_date=d, action_type="cash", amount=D(a),
                               source="eodhd") for d, a in rows]  # fmt: skip
    VersionedStore(data_dir / "clean").append(ACTIONS, [action_row(a, t) for a in actions], t)


def confirm_matches(tmp_path: Path, answer: str) -> Result:
    args = ["data", "issuer-check", "--folder", str(tmp_path / "issuer"), "--data-dir",
            str(tmp_path), "--state-dir", str(tmp_path / "state"), "--confirm-matches"]  # fmt: skip
    return CliRunner().invoke(app, args, input=answer)


def test_confirm_matches_records_the_exact_matches_after_one_question(tmp_path: Path) -> None:
    """v1.11: the exact match is listed and recorded after one "y"; the row that differs (by
    0.00001) is never part of the batch, and a second run asks nothing."""
    (tmp_path / "issuer").mkdir()
    dec2024 = "Income,Dec 30 2024,Dec 30 2024,Jan 07 2025,$0.40000,—,$0.40000\n"
    write(tmp_path / "issuer", "VCN.csv", VANGUARD_CSV + dec2024)
    eodhd_cash(tmp_path, (date(2025, 12, 30), "0.38809"), (date(2024, 12, 30), "0.40001"))
    result = confirm_matches(tmp_path, "y\n")
    assert "VCN 2024-12-30: issuer cash 0.40000, reinvested 0; vendor cash 0.40001: differs" in (
        result.output
    )
    assert "Exact matches not yet confirmed for Q10 (1):" in result.output
    assert "Record a Q10 confirmation for this row?" in result.output
    lines = (tmp_path / "state" / "confirmations.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert [(r["symbol"], r["session_date"], r["check"]) for r in records] == [
        ("VCN", "2025-12-30", "Q10"),
    ]  # fmt: skip
    assert "eodhd cash 0.38809; exact match" in records[0]["reason"]
    again = confirm_matches(tmp_path, "")
    assert "Already confirmed for Q10: 1 of the exact matches." in again.output
    assert "Record a Q10" not in again.output


def test_confirm_matches_records_nothing_on_no(tmp_path: Path) -> None:
    (tmp_path / "issuer").mkdir()
    write(tmp_path / "issuer", "VCN.csv", VANGUARD_CSV)
    eodhd_cash(tmp_path, (date(2025, 12, 30), "0.38809"))
    result = confirm_matches(tmp_path, "n\n")
    assert "Record a Q10 confirmation for this row?" in result.output
    assert "Nothing recorded." in result.output
    assert not (tmp_path / "state" / "confirmations.jsonl").exists()
