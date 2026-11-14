import csv
from datetime import date, datetime, timedelta
import io
from time import perf_counter
import tracemalloc
import zipfile

import pytest
from openpyxl import Workbook

from backend.app import import_calls
from backend.app.import_calls import CallImportError, parse_calls_import
from backend.app.pipeline import PipelineHistoryObservation, PipelinePlannedCalls


HEADER = ("date", "hour", "group_id", "calls")


def csv_bytes(rows, *, header=HEADER, bom=False):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    text = stream.getvalue()
    return (("\ufeff" if bom else "") + text).encode("utf-8")


def xlsx_bytes(rows, *, sheet_names=("calls",), formula=None):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_names[0]
    for row in rows:
        sheet.append(row)
    for name in sheet_names[1:]:
        workbook.create_sheet(name)
    if formula is not None:
        sheet[formula[0]] = formula[1]
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def rewrite_xlsx(
    data,
    *,
    updates=None,
    additions=None,
    removals=(),
    compression=zipfile.ZIP_DEFLATED,
):
    updates = updates or {}
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(
        output, "w", compression=compression
    ) as target:
        for item in source.infolist():
            if item.filename in removals:
                continue
            target.writestr(item.filename, updates.get(item.filename, source.read(item)))
        for name, content in additions or ():
            target.writestr(name, content)
    return output.getvalue()


def rename_worksheet_xlsx(data, new_name, *, worksheet_xml=None, remove_original=False):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        relationships = archive.read("xl/_rels/workbook.xml.rels")
        original_sheet = archive.read("xl/worksheets/sheet1.xml")
    relationships = relationships.replace(
        b'Target="/xl/worksheets/sheet1.xml"',
        f'Target="/xl/worksheets/{new_name}"'.encode(),
    )
    if worksheet_xml is None:
        worksheet_xml = original_sheet
    return rewrite_xlsx(
        data,
        updates={"xl/_rels/workbook.xml.rels": relationships},
        additions=[(f"xl/worksheets/{new_name}", worksheet_xml)],
        removals=("xl/worksheets/sheet1.xml",) if remove_original else (),
    )


def add_shared_strings_xlsx(
    data,
    strings,
    *,
    shared_strings_xml=None,
    compression=zipfile.ZIP_DEFLATED,
):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        relationships = archive.read("xl/_rels/workbook.xml.rels")
        content_types = archive.read("[Content_Types].xml")
        worksheet = archive.read("xl/worksheets/sheet1.xml")
    worksheet = worksheet.replace(
        b'<c r="A1" t="inlineStr"><is><t>date</t></is></c>',
        b'<c r="A1" t="s"><v>0</v></c>',
    )
    relationships = relationships.replace(
        b"</Relationships>",
        b'<Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
        b'Target="/xl/sharedStrings.xml" Id="rId99"/></Relationships>',
    )
    content_types = content_types.replace(
        b"</Types>",
        b'<Override PartName="/xl/sharedStrings.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>',
    )
    if shared_strings_xml is None:
        shared_strings_xml = (
            f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            f'count="1" uniqueCount="{len(strings)}">'
            + "".join(f"<si><t>{value}</t></si>" for value in strings)
            + "</sst>"
        ).encode()
    return rewrite_xlsx(
        data,
        updates={
            "xl/_rels/workbook.xml.rels": relationships,
            "[Content_Types].xml": content_types,
            "xl/worksheets/sheet1.xml": worksheet,
        },
        additions=[("xl/sharedStrings.xml", shared_strings_xml)],
        compression=compression,
    )


def assert_xlsx_rejected_before_openpyxl(data, monkeypatch, message):
    openpyxl_called = False

    def unexpected_openpyxl_call(*args, **kwargs):
        nonlocal openpyxl_called
        openpyxl_called = True
        pytest.fail("XML preflight did not reject the package before openpyxl.")

    monkeypatch.setattr(import_calls, "load_workbook", unexpected_openpyxl_call)
    tracemalloc.start()
    started = perf_counter()
    try:
        with pytest.raises(CallImportError, match=message) as error:
            parse_calls_import(
                data,
                file_format="xlsx",
                mode="planned_calls",
                group_ids=["sales"],
            )
    finally:
        elapsed = perf_counter() - started
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    assert not openpyxl_called
    assert peak_bytes < 16 * 1024 * 1024
    assert elapsed < 5
    assert "SENSITIVE" not in str(error.value)


def assert_xlsx_rejected_before_iterparse(data, monkeypatch, message):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        target_xml = archive.read("docProps/extra.xml")
    original_iterparse = import_calls.iterparse

    def fail_on_target_iterparse(source, *args, **kwargs):
        if source.getvalue() == target_xml:
            pytest.fail("Raw XML preflight did not reject before iterparse.")
        return original_iterparse(source, *args, **kwargs)

    monkeypatch.setattr(import_calls, "iterparse", fail_on_target_iterparse)
    assert_xlsx_rejected_before_openpyxl(data, monkeypatch, message)


@pytest.mark.parametrize("bom", [False, True])
def test_csv_history_returns_existing_models_and_preserves_zero_and_unicode(bom):
    data = csv_bytes(
        [
            ("2026-10-05", "8", "販売", "0"),
            ("2026-10-12", "8", "販売", "12"),
        ],
        bom=bom,
    )

    records = parse_calls_import(
        data, file_format="csv", mode="history", group_ids=["販売"]
    )

    assert all(isinstance(record, PipelineHistoryObservation) for record in records)
    assert [(item.date, item.hour, item.group_id, item.calls) for item in records] == [
        (date(2026, 10, 5), 8, "販売", 0),
        (date(2026, 10, 12), 8, "販売", 12),
    ]


def test_csv_planned_calls_maps_calls_to_pipeline_field_and_keeps_fraction():
    records = parse_calls_import(
        csv_bytes(
            [
                ("2026-11-02", "8", "sales", "2.5"),
                ("2026-11-02", "9", "sales", "0"),
            ]
        ),
        file_format="csv",
        mode="planned_calls",
        group_ids=["sales"],
    )

    assert all(isinstance(record, PipelinePlannedCalls) for record in records)
    assert [item.calls_per_hour for item in records] == [2.5, 0.0]


@pytest.mark.parametrize(
    ("mode", "rows", "model_type"),
    [
        (
            "history",
            [
                [datetime(2026, 10, 5), 8, "sales", 0],
                [date(2026, 10, 12), 8, "sales", 2],
            ],
            PipelineHistoryObservation,
        ),
        (
            "planned_calls",
            [[date(2026, 11, 2), 8, "sales", 2.5]],
            PipelinePlannedCalls,
        ),
    ],
)
def test_xlsx_parses_date_cells_and_returns_pipeline_models(mode, rows, model_type):
    records = parse_calls_import(
        xlsx_bytes([HEADER, *rows]),
        file_format="xlsx",
        mode=mode,
        group_ids=["sales"],
    )

    assert all(isinstance(record, model_type) for record in records)


def test_csv_group_is_trimmed_but_must_be_selected():
    records = parse_calls_import(
        csv_bytes([("2026-10-05", "8", " sales ", "1")]),
        file_format="csv",
        mode="history",
        group_ids=["sales"],
    )

    assert records[0].group_id == "sales"


@pytest.mark.parametrize(
    "header",
    [
        ("date", "hour", "group_id"),
        ("date", "hour", "group_id", "calls", "extra"),
        ("Date", "hour", "group_id", "calls"),
        ("hour", "date", "group_id", "calls"),
    ],
)
def test_csv_rejects_missing_unknown_or_misordered_headers(header):
    with pytest.raises(CallImportError, match="header"):
        parse_calls_import(
            csv_bytes([], header=header),
            file_format="csv",
            mode="history",
            group_ids=["sales"],
        )


@pytest.mark.parametrize(
    ("mode", "row", "sensitive_value"),
    [
        ("history", ("2026-11-02", "23", "sales", "1"), "23"),
        (
            "history",
            ("2026-11-02", "8", "private_group_value", "1"),
            "private_group_value",
        ),
        ("history", ("2026-11-02", "8", "sales", "-1"), "-1"),
        ("history", ("private_date_value", "8", "sales", "1"), "private_date_value"),
        ("history", ("2026-11-02", "8", "sales", ""), None),
        ("planned_calls", ("2026-11-02", "8", "sales", "-1.5"), "-1.5"),
        ("planned_calls", ("2026-11-02", "8", "sales", "inf"), "inf"),
    ],
)
def test_csv_rejects_invalid_rows_with_safe_row_number(mode, row, sensitive_value):
    with pytest.raises(CallImportError) as error:
        parse_calls_import(
            csv_bytes([row]),
            file_format="csv",
            mode=mode,
            group_ids=["sales"],
        )

    assert "row 2" in str(error.value).lower()
    if sensitive_value:
        assert sensitive_value not in str(error.value)


def test_csv_rejects_duplicates_after_group_normalization():
    data = csv_bytes(
        [
            ("2026-10-05", "8", "sales", "1"),
            ("2026-10-05", "8", " sales ", "2"),
        ]
    )

    with pytest.raises(CallImportError, match="duplicate"):
        parse_calls_import(
            data, file_format="csv", mode="history", group_ids=["sales"]
        )


@pytest.mark.parametrize(
    "row",
    [
        b"2026-10-05,8,sales\n",
        b"2026-10-05,8,sales,1,extra\n",
    ],
)
def test_csv_rejects_rows_with_missing_or_extra_values(row):
    data = b"date,hour,group_id,calls\n" + row

    with pytest.raises(CallImportError, match="four values"):
        parse_calls_import(
            data, file_format="csv", mode="history", group_ids=["sales"]
        )


def test_csv_rejects_empty_input_rows_and_malformed_quotes():
    with pytest.raises(CallImportError, match="no data"):
        parse_calls_import(
            csv_bytes([]), file_format="csv", mode="history", group_ids=["sales"]
        )

    with pytest.raises(CallImportError, match="row"):
        parse_calls_import(
            b'date,hour,group_id,calls\n"2026-10-05,8,sales,1\n',
            file_format="csv",
            mode="history",
            group_ids=["sales"],
        )


def test_csv_does_not_evaluate_formula_like_group_values():
    formula = "=1+1"
    records = parse_calls_import(
        csv_bytes([("2026-10-05", "8", formula, "1")]),
        file_format="csv",
        mode="history",
        group_ids=[formula],
    )

    assert records[0].group_id == formula


def test_xlsx_rejects_formula_cells_without_echoing_formula():
    formula = "=SENSITIVE_FORMULA_VALUE"
    with pytest.raises(CallImportError) as error:
        parse_calls_import(
            xlsx_bytes(
                [HEADER, ["2026-11-02", 8, "sales", 1]],
                formula=("D2", formula),
            ),
            file_format="xlsx",
            mode="planned_calls",
            group_ids=["sales"],
        )

    assert "formula" in str(error.value).lower()
    assert formula not in str(error.value)


def test_xlsx_rejects_formula_in_header():
    with pytest.raises(CallImportError, match="formula"):
        parse_calls_import(
            xlsx_bytes(
                [HEADER, ["2026-11-02", 8, "sales", 1]],
                formula=("A1", '="date"'),
            ),
            file_format="xlsx",
            mode="planned_calls",
            group_ids=["sales"],
        )


def test_xlsx_rejects_formula_in_relationship_target_worksheet():
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    formula_workbook = xlsx_bytes(
        [HEADER, ["2026-11-02", 8, "sales", 1]],
        formula=("E2", "=1+1"),
    )
    with zipfile.ZipFile(io.BytesIO(formula_workbook)) as archive:
        formula_worksheet = archive.read("xl/worksheets/sheet1.xml")
    data = rename_worksheet_xlsx(
        base, "odd.xml", worksheet_xml=formula_worksheet, remove_original=True
    )

    with pytest.raises(CallImportError, match="formula"):
        parse_calls_import(
            data,
            file_format="xlsx",
            mode="planned_calls",
            group_ids=["sales"],
        )


def test_xlsx_accepts_a_benign_renamed_worksheet():
    data = rename_worksheet_xlsx(
        xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]]),
        "odd.xml",
        remove_original=True,
    )

    records = parse_calls_import(
        data,
        file_format="xlsx",
        mode="planned_calls",
        group_ids=["sales"],
    )

    assert len(records) == 1
    assert records[0].group_id == "sales"


def test_xlsx_enforces_row_limit_in_relationship_target_worksheet():
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    rows = [HEADER, *([["2026-11-02", 8, "sales", 1]] * 1_611)]
    long_workbook = xlsx_bytes(rows)
    with zipfile.ZipFile(io.BytesIO(long_workbook)) as archive:
        long_worksheet = archive.read("xl/worksheets/sheet1.xml")
    data = rename_worksheet_xlsx(
        base, "odd.xml", worksheet_xml=long_worksheet, remove_original=True
    )

    with pytest.raises(CallImportError, match="maximum"):
        parse_calls_import(
            data,
            file_format="xlsx",
            mode="planned_calls",
            group_ids=["sales"],
        )


def test_xlsx_rejects_shared_strings_over_count_limit_before_loading(monkeypatch):
    strings = ["date", *(f"{index:05x}" for index in range(40_004))]
    data = add_shared_strings_xlsx(
        xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]]),
        strings,
    )

    assert_xlsx_rejected_before_openpyxl(data, monkeypatch, "shared strings")


def test_xlsx_rejects_shared_strings_over_character_limit_before_loading(
    monkeypatch,
):
    data = add_shared_strings_xlsx(
        xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]]),
        ["date", "x" * 200_001],
        compression=zipfile.ZIP_STORED,
    )

    assert_xlsx_rejected_before_openpyxl(data, monkeypatch, "shared strings")


def test_xlsx_rejects_shared_string_rich_runs_before_openpyxl(monkeypatch):
    rich_runs = b"<r/>" * 100_000
    shared_strings = (
        b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        b'count="1" uniqueCount="2"><si><t>date</t></si><si>'
        + rich_runs
        + b"</si></sst>"
    )
    data = add_shared_strings_xlsx(
        xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]]),
        [],
        shared_strings_xml=shared_strings,
        compression=zipfile.ZIP_STORED,
    )
    assert len(data) < 2 * 1024 * 1024

    assert_xlsx_rejected_before_openpyxl(data, monkeypatch, "rich text")


def test_xlsx_rejects_shared_strings_manifest_path_mismatch(monkeypatch):
    base = add_shared_strings_xlsx(
        xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]]),
        ["date"],
    )
    with zipfile.ZipFile(io.BytesIO(base)) as archive:
        content_types = archive.read("[Content_Types].xml")
    content_types = content_types.replace(
        b'PartName="/xl/sharedStrings.xml"',
        b'PartName="/xl/sharedStrings.bin"',
    )
    data = rewrite_xlsx(
        base,
        updates={"[Content_Types].xml": content_types},
        additions=[
            (
                "xl/sharedStrings.bin",
                b'<sst><si><r><t>SENSITIVE</t></r></si></sst>',
            )
        ],
    )

    assert_xlsx_rejected_before_openpyxl(data, monkeypatch, "shared strings")


def test_xlsx_rejects_duplicate_workbook_manifest_parts(monkeypatch):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    with zipfile.ZipFile(io.BytesIO(base)) as archive:
        content_types = archive.read("[Content_Types].xml")
    duplicate_workbook = (
        b'<Override PartName="/xl/workbook.bin" '
        b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    )
    content_types = content_types.replace(
        b'<Override PartName="/xl/workbook.xml"',
        duplicate_workbook + b'<Override PartName="/xl/workbook.xml"',
        1,
    )
    data = rewrite_xlsx(
        base,
        updates={"[Content_Types].xml": content_types},
        additions=[("xl/workbook.bin", b"<workbook/>")],
    )

    assert_xlsx_rejected_before_openpyxl(data, monkeypatch, "workbook")


def test_xlsx_rejects_oversized_styles_xml_before_openpyxl(monkeypatch):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    with zipfile.ZipFile(io.BytesIO(base)) as archive:
        styles = archive.read("xl/styles.xml")
    style_records = b"".join(
        f'<xf numFmtId="0" fontId="{index}" fillId="0" borderId="0" xfId="0"/>'.encode()
        for index in range(65_000)
    )
    styles = styles.replace(
        b'<cellXfs count="1">',
        b'<cellXfs count="65001">',
    ).replace(b"</cellXfs>", style_records + b"</cellXfs>")
    data = rewrite_xlsx(
        base,
        updates={"xl/styles.xml": styles},
    )
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo("xl/styles.xml")
        assert info.file_size > 2 * 1024 * 1024
        assert info.file_size <= info.compress_size * 100
    assert len(data) < 2 * 1024 * 1024

    assert_xlsx_rejected_before_openpyxl(data, monkeypatch, "XML part")


def test_xlsx_rejects_styles_with_too_many_elements_before_openpyxl(monkeypatch):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    with zipfile.ZipFile(io.BytesIO(base)) as archive:
        styles = archive.read("xl/styles.xml")
    styles = styles.replace(
        b'<cellXfs count="1">',
        b'<cellXfs count="65001">',
    ).replace(b"</cellXfs>", b"<xf/>" * 65_000 + b"</cellXfs>")
    assert len(styles) < 512 * 1024
    data = rewrite_xlsx(
        base,
        updates={"xl/styles.xml": styles},
        compression=zipfile.ZIP_STORED,
    )
    assert len(data) < 2 * 1024 * 1024

    assert_xlsx_rejected_before_openpyxl(data, monkeypatch, "styles XML")


def test_xlsx_rejects_xml_element_with_too_many_attributes_before_openpyxl(
    monkeypatch,
):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    with zipfile.ZipFile(io.BytesIO(base)) as archive:
        styles = archive.read("xl/styles.xml")
    attributes = b" ".join(
        f'custom{index}="x"'.encode() for index in range(65)
    )
    styles = styles.replace(
        b"</cellXfs>",
        b"<xf " + attributes + b"/>" + b"</cellXfs>",
    )
    data = rewrite_xlsx(
        base,
        updates={"xl/styles.xml": styles},
    )

    assert_xlsx_rejected_before_openpyxl(data, monkeypatch, "attributes")


def test_xlsx_rejects_excessive_namespace_declarations_before_openpyxl(monkeypatch):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    declarations = b" ".join(
        f'xmlns:p{index}="urn:test:{index}"'.encode() for index in range(65)
    )
    data = rewrite_xlsx(
        base, updates={"xl/styles.xml": b"<root " + declarations + b"/>"}
    )
    assert_xlsx_rejected_before_openpyxl(data, monkeypatch, "attributes")


def test_xlsx_rejects_100001_namespace_markers_before_iterparse(monkeypatch):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    declarations = b" ".join(
        f'xmlns:p{index}="u"'.encode() for index in range(100_001)
    )
    xml_part = b"<root " + declarations + b"/>"
    assert len(xml_part) < 2 * 1024 * 1024
    data = rewrite_xlsx(base, additions=[("docProps/extra.xml", xml_part)])

    assert_xlsx_rejected_before_iterparse(data, monkeypatch, "namespace")


def test_xlsx_rejects_ordinary_attribute_bomb_before_iterparse(monkeypatch):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    attributes = b" ".join(
        f'a{index}="x"'.encode() for index in range(100_001)
    )
    xml_part = b"<root " + attributes + b"/>"
    assert len(xml_part) < 2 * 1024 * 1024
    data = rewrite_xlsx(base, additions=[("docProps/extra.xml", xml_part)])

    assert_xlsx_rejected_before_iterparse(data, monkeypatch, "markup")


def test_xlsx_rejects_distributed_namespace_markers_per_xml_part(monkeypatch):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    children = b"".join(
        f'<e xmlns:p{index}="u"/>'.encode() for index in range(257)
    )
    data = rewrite_xlsx(
        base,
        additions=[("docProps/extra.xml", b"<root>" + children + b"</root>")],
    )

    assert_xlsx_rejected_before_iterparse(data, monkeypatch, "namespace")


def test_xlsx_accepts_256_namespace_markers_per_xml_part():
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    children = b"".join(
        f'<e xmlns:p{index}="u"/>'.encode() for index in range(256)
    )
    data = rewrite_xlsx(
        base,
        additions=[("docProps/extra.xml", b"<root>" + children + b"</root>")],
    )

    records = parse_calls_import(
        data,
        file_format="xlsx",
        mode="planned_calls",
        group_ids=["sales"],
    )

    assert len(records) == 1


def test_xlsx_rejects_oversized_quote_aware_markup_token_before_iterparse(
    monkeypatch,
):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    value = b"".join(f"{index:04x}".encode() for index in range(2_048))
    value = value[:4_096] + b">" + value[4_097:]
    xml_part = b'<root value="' + value + b'"/>'
    data = rewrite_xlsx(base, additions=[("docProps/extra.xml", xml_part)])

    assert_xlsx_rejected_before_iterparse(data, monkeypatch, "markup")


@pytest.mark.parametrize(
    "xml_part",
    [
        pytest.param(b"<root>\x00</root>", id="nul-byte"),
        pytest.param(b"<root>\xe9</root>", id="non-utf8-bytes"),
        pytest.param(b"<root/>".decode().encode("utf-16"), id="utf-16"),
        pytest.param(b"<root/>".decode().encode("utf-32"), id="utf-32"),
        pytest.param(
            b'<?xml version="1.0" encoding="ISO-8859-1"?><root/>',
            id="non-utf8-declaration",
        ),
    ],
)
def test_xlsx_rejects_non_utf8_xml_before_iterparse(monkeypatch, xml_part):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    data = rewrite_xlsx(base, additions=[("docProps/extra.xml", xml_part)])

    assert_xlsx_rejected_before_iterparse(data, monkeypatch, "UTF-8|NUL")


def test_xlsx_rejects_literal_namespace_marker_false_positive_in_comment(
    monkeypatch,
):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    comment = b"<!--" + b"xmlns " * 257 + b"-->"
    data = rewrite_xlsx(
        base,
        additions=[("docProps/extra.xml", comment + b"<root/>")],
    )

    assert_xlsx_rejected_before_iterparse(data, monkeypatch, "namespace")


def test_xlsx_accepts_utf8_bom_escaped_text_and_standard_xml_declaration():
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "R&D", 1]])
    with zipfile.ZipFile(io.BytesIO(base)) as archive:
        worksheet = archive.read("xl/worksheets/sheet1.xml")
    assert b"R&amp;D" in worksheet
    data = rewrite_xlsx(
        base,
        updates={"xl/worksheets/sheet1.xml": b"\xef\xbb\xbf" + worksheet},
    )

    records = parse_calls_import(
        data,
        file_format="xlsx",
        mode="planned_calls",
        group_ids=["R&D"],
    )

    assert records[0].group_id == "R&D"


def test_xlsx_markup_scan_distinguishes_comment_cdata_and_processing_instruction():
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    xml_part = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b"<root><!-- > < -->"
        b"<![CDATA[< >]]><?probe a > b?></root>"
    )
    data = rewrite_xlsx(base, additions=[("docProps/extra.xml", xml_part)])

    records = parse_calls_import(
        data,
        file_format="xlsx",
        mode="planned_calls",
        group_ids=["sales"],
    )

    assert len(records) == 1


def test_xlsx_rejects_less_than_inside_quoted_attribute_via_xml_parser(monkeypatch):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    data = rewrite_xlsx(
        base,
        additions=[("docProps/extra.xml", b'<root value="left < right"/>')],
    )

    assert_xlsx_rejected_before_openpyxl(data, monkeypatch, "XML part")


def test_xlsx_continues_rejecting_dtd_and_entity_declarations(monkeypatch):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    xml_part = b'<!DOCTYPE root [<!ENTITY item "value">]><root>&item;</root>'
    data = rewrite_xlsx(base, additions=[("docProps/extra.xml", xml_part)])

    assert_xlsx_rejected_before_openpyxl(data, monkeypatch, "XML part")


def test_xlsx_enforces_global_xml_element_limit_before_openpyxl(monkeypatch):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    xml_part = b"<root>" + b"<e/>" * 4_000 + b"</root>"
    data = rewrite_xlsx(
        base,
        additions=[
            (f"docProps/extra{index}.xml", xml_part)
            for index in range(38)
        ],
        compression=zipfile.ZIP_STORED,
    )
    assert len(data) < 2 * 1024 * 1024

    assert_xlsx_rejected_before_openpyxl(
        data,
        monkeypatch,
        "XML element count",
    )


def test_xlsx_enforces_global_xml_attribute_limit_before_openpyxl(monkeypatch):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    with zipfile.ZipFile(io.BytesIO(base)) as archive:
        theme = archive.read("xl/theme/theme1.xml")
    attributes = b" ".join(
        f'custom{index}="x"'.encode() for index in range(63)
    )
    extension = b'<a:ext uri="urn:test" ' + attributes + b'/>'
    theme = theme.replace(b"</a:theme>", extension * 1_600 + b"</a:theme>")
    data = rewrite_xlsx(
        base,
        updates={"xl/theme/theme1.xml": theme},
        compression=zipfile.ZIP_STORED,
    )
    assert len(data) < 2 * 1024 * 1024

    assert_xlsx_rejected_before_openpyxl(
        data,
        monkeypatch,
        "XML attribute count",
    )


def test_xlsx_rejects_excessive_xml_depth_before_openpyxl(monkeypatch):
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    with zipfile.ZipFile(io.BytesIO(base)) as archive:
        theme = archive.read("xl/theme/theme1.xml")
    nested = b"<a:n>" * 65 + b"</a:n>" * 65
    theme = (
        b'<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        + nested
        + b"</a:theme>"
    )
    data = rewrite_xlsx(
        base,
        updates={"xl/theme/theme1.xml": theme},
    )

    assert_xlsx_rejected_before_openpyxl(data, monkeypatch, "depth")


def test_xlsx_rejects_malformed_eager_xml_with_sanitized_error(monkeypatch):
    secret = b"SENSITIVE_MALFORMED_THEME"
    data = rewrite_xlsx(
        xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]]),
        updates={"xl/theme/theme1.xml": b"<a:theme>" + secret},
    )

    assert_xlsx_rejected_before_openpyxl(data, monkeypatch, "XML part")


def test_xlsx_rejects_calls_outside_finite_float_range_safely():
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    with zipfile.ZipFile(io.BytesIO(base)) as archive:
        worksheet = archive.read("xl/worksheets/sheet1.xml")
    huge_number = "9" * 400
    worksheet = worksheet.replace(
        b'<c r="D2" t="n"><v>1</v></c>',
        f'<c r="D2" t="n"><v>{huge_number}</v></c>'.encode(),
    )
    data = rewrite_xlsx(
        base, updates={"xl/worksheets/sheet1.xml": worksheet}
    )

    with pytest.raises(CallImportError) as error:
        parse_calls_import(
            data,
            file_format="xlsx",
            mode="planned_calls",
            group_ids=["sales"],
        )

    assert "row 2" in str(error.value).lower()
    assert "calls" in str(error.value).lower()


def test_xlsx_rejects_macro_content_and_external_link_parts():
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    with zipfile.ZipFile(io.BytesIO(base)) as archive:
        content_types = archive.read("[Content_Types].xml")
    macro_content_types = content_types.replace(
        b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        b"application/vnd.ms-excel.sheet.macroEnabled.main+xml",
    )
    macro_file = rewrite_xlsx(
        base, updates={"[Content_Types].xml": macro_content_types}
    )
    external_file = rewrite_xlsx(
        base,
        additions=[("xl/externalLinks/externalLink1.xml", b"<externalLink/>")],
    )

    for data, message in ((macro_file, "macro"), (external_file, "external")):
        with pytest.raises(CallImportError, match=message):
            parse_calls_import(
                data,
                file_format="xlsx",
                mode="planned_calls",
                group_ids=["sales"],
            )


def test_xlsx_rejects_external_relationships_and_unsafe_zip_paths():
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    rels = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Target="https://example.invalid/book.xlsx" '
        b'TargetMode="External" Id="rId1" Type="externalLink"/></Relationships>'
    )
    external_rel = rewrite_xlsx(
        base, additions=[("xl/worksheets/_rels/sheet1.xml.rels", rels)]
    )
    unsafe_path = rewrite_xlsx(base, additions=[("../outside.xml", b"ignored")])
    drive_relative_path = rewrite_xlsx(
        base, additions=[("C:../outside.xml", b"ignored")]
    )

    with pytest.raises(CallImportError, match="external"):
        parse_calls_import(
            external_rel,
            file_format="xlsx",
            mode="planned_calls",
            group_ids=["sales"],
        )
    with pytest.raises(CallImportError, match="path"):
        parse_calls_import(
            unsafe_path,
            file_format="xlsx",
            mode="planned_calls",
            group_ids=["sales"],
        )
    with pytest.raises(CallImportError, match="path"):
        parse_calls_import(
            drive_relative_path,
            file_format="xlsx",
            mode="planned_calls",
            group_ids=["sales"],
        )


def test_xlsx_rejects_an_extra_sheet():
    with pytest.raises(CallImportError, match="sheet"):
        parse_calls_import(
            xlsx_bytes(
                [HEADER, ["2026-11-02", 8, "sales", 1]],
                sheet_names=("calls", "extra"),
            ),
            file_format="xlsx",
            mode="planned_calls",
            group_ids=["sales"],
        )


@pytest.mark.parametrize(
    "part_name",
    ["xl/worksheets/unused.xml", "XL/WORKSHEETS/unused.xml"],
)
def test_xlsx_rejects_an_unreferenced_worksheet_part(part_name):
    data = rewrite_xlsx(
        xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]]),
        additions=[(part_name, b"<worksheet/>")],
    )

    with pytest.raises(CallImportError, match="worksheet"):
        parse_calls_import(
            data,
            file_format="xlsx",
            mode="planned_calls",
            group_ids=["sales"],
        )


def test_xlsx_enforces_workbook_metadata_size_limit():
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    with zipfile.ZipFile(io.BytesIO(base)) as archive:
        workbook_xml = archive.read("xl/workbook.xml")
    workbook_xml = workbook_xml.replace(
        b"</workbook>",
        b"<!--" + b"x" * (1024 * 1024) + b"--></workbook>",
    )
    data = rewrite_xlsx(
        base,
        updates={"xl/workbook.xml": workbook_xml},
        compression=zipfile.ZIP_STORED,
    )

    with pytest.raises(CallImportError, match="metadata"):
        parse_calls_import(
            data,
            file_format="xlsx",
            mode="planned_calls",
            group_ids=["sales"],
        )


def test_xlsx_rejects_zip_bombs_and_too_many_entries():
    base = xlsx_bytes([HEADER, ["2026-11-02", 8, "sales", 1]])
    bomb = rewrite_xlsx(
        base,
        additions=[("large.txt", b"x" * 200_000)],
    )
    too_many = rewrite_xlsx(
        base,
        additions=[(f"extra/{index}.txt", b"x") for index in range(101)],
        compression=zipfile.ZIP_STORED,
    )

    for data in (bomb, too_many):
        with pytest.raises(CallImportError):
            parse_calls_import(
                data,
                file_format="xlsx",
                mode="planned_calls",
                group_ids=["sales"],
            )


def test_xlsx_enforces_pipeline_row_limits():
    rows = [HEADER, *([["2026-11-02", 8, "sales", 1]] * 1_611)]

    with pytest.raises(CallImportError, match="maximum"):
        parse_calls_import(
            xlsx_bytes(rows),
            file_format="xlsx",
            mode="planned_calls",
            group_ids=["sales"],
        )


def test_import_rejects_oversized_files_and_path_inputs_without_reading_them(tmp_path):
    path = tmp_path / "calls.csv"
    path.write_text("private data", encoding="utf-8")

    for data in (b"x" * (2 * 1024 * 1024 + 1), path):
        with pytest.raises(CallImportError):
            parse_calls_import(
                data,
                file_format="csv",
                mode="history",
                group_ids=["sales"],
            )


@pytest.mark.parametrize(
    ("mode", "limit"),
    [("history", 10_000), ("planned_calls", 1_610)],
)
def test_csv_enforces_pipeline_row_limits(mode, limit):
    day = date(2000, 1, 1)
    rows = [
        ((day + timedelta(days=index)).isoformat(), "8", "sales", "1")
        for index in range(limit + 1)
    ]

    with pytest.raises(CallImportError, match="maximum"):
        parse_calls_import(
            csv_bytes(rows),
            file_format="csv",
            mode=mode,
            group_ids=["sales"],
        )


def test_xlsx_error_is_sanitized_for_invalid_bytes():
    secret = b"PRIVATE_INVALID_WORKBOOK"
    with pytest.raises(CallImportError) as error:
        parse_calls_import(
            secret,
            file_format="xlsx",
            mode="planned_calls",
            group_ids=["sales"],
        )

    assert secret.decode() not in str(error.value)
    assert "xlsx" in str(error.value).lower()


def test_parser_rejects_invalid_mode_format_and_group_list():
    data = csv_bytes([("2026-10-05", "8", "sales", "1")])
    for arguments in (
        {"mode": "other", "file_format": "csv", "group_ids": ["sales"]},
        {"mode": "history", "file_format": "xls", "group_ids": ["sales"]},
        {"mode": "history", "file_format": "csv", "group_ids": []},
    ):
        with pytest.raises(CallImportError):
            parse_calls_import(data, **arguments)
