"""Bounded in-memory import of hourly call rows from CSV or XLSX."""

from collections.abc import Collection, Iterable, Sequence
import csv
from datetime import date, datetime, time
import io
import math
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Literal
import zipfile

from defusedxml.ElementTree import fromstring, iterparse
from openpyxl import load_workbook
from openpyxl.xml import DEFUSEDXML
from pydantic import ValidationError

from .pipeline import (
    MAX_HISTORY_ROWS,
    MAX_HOURLY_CALL_ROWS,
    PipelineHistoryObservation,
    PipelinePlannedCalls,
)
from .solver_v2 import MAX_GROUPS, MAX_IDENTIFIER_LENGTH


HEADER = ("date", "hour", "group_id", "calls")
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 10_000
MAX_XLSX_ENTRIES = 100
MAX_XLSX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_XLSX_ENTRY_BYTES = 10 * 1024 * 1024
MAX_XLSX_METADATA_BYTES = 1024 * 1024
MAX_XLSX_COMPRESSION_RATIO = 100
MAX_XLSX_XML_PART_BYTES = 2 * 1024 * 1024
MAX_XLSX_XML_ELEMENTS = 150_000
MAX_XLSX_XML_ELEMENTS_PER_EAGER_PART = 4_096
MAX_XLSX_WORKSHEET_XML_ELEMENTS = 120_000
MAX_XLSX_SHARED_STRING_XML_ELEMENTS = 50_000
MAX_XLSX_XML_DEPTH = 64
MAX_XLSX_XML_ATTRIBUTES = 100_000
MAX_XLSX_XML_ATTRIBUTES_PER_ELEMENT = 64
MAX_XLSX_XML_NAMESPACE_MARKERS = 256
MAX_XLSX_XML_MARKUP_TOKEN_BYTES = 8 * 1024
MAX_XLSX_SHARED_STRINGS = 20_000
MAX_XLSX_SHARED_STRING_CHARS = 200_000
_INTEGER = re.compile(r"^[+-]?[0-9]+$")
_CELL_REFERENCE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")
_XML_DECLARATION = re.compile(rb"\A<\?xml\s+([^?]*)\?>")
_XML_ENCODING = re.compile(rb"\bencoding\s*=\s*(['\"])(.*?)\1")
_XLSX_WORKBOOK_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
)
_XLSX_SHARED_STRINGS_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
)
_RELATIONSHIPS_ID = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
)

CallImportMode = Literal["history", "planned_calls"]
CallImportFormat = Literal["csv", "xlsx"]
CallImportRecord = PipelineHistoryObservation | PipelinePlannedCalls


class CallImportError(ValueError):
    """A safe, user-facing import validation error."""


def parse_calls_import(
    data: bytes,
    *,
    file_format: CallImportFormat,
    mode: CallImportMode,
    group_ids: Collection[str],
) -> list[CallImportRecord]:
    """Parse in-memory call rows into the matching existing pipeline models."""
    if not isinstance(data, bytes):
        raise CallImportError("Import data must be provided as bytes.")
    if len(data) > MAX_FILE_BYTES:
        raise CallImportError("Import file exceeds the 2 MiB size limit.")
    if file_format not in ("csv", "xlsx"):
        raise CallImportError("File format must be CSV or XLSX.")
    if mode not in ("history", "planned_calls"):
        raise CallImportError("Mode must be history or planned_calls.")

    allowed_groups = _normalize_groups(group_ids)
    max_rows = min(
        MAX_IMPORT_ROWS,
        MAX_HISTORY_ROWS if mode == "history" else MAX_HOURLY_CALL_ROWS,
    )
    if file_format == "csv":
        return _parse_csv(data, mode, allowed_groups, max_rows)
    return _parse_xlsx(data, mode, allowed_groups, max_rows)


def _normalize_groups(group_ids: Collection[str]) -> set[str]:
    if (
        isinstance(group_ids, (str, bytes))
        or not isinstance(group_ids, Collection)
        or not 1 <= len(group_ids) <= MAX_GROUPS
    ):
        raise CallImportError("Selected groups must contain 1 to 10 group IDs.")

    normalized = []
    for group_id in group_ids:
        if not isinstance(group_id, str):
            raise CallImportError("Selected group IDs must be text.")
        group_id = group_id.strip()
        if not group_id or len(group_id) > MAX_IDENTIFIER_LENGTH:
            raise CallImportError("Selected group ID is invalid.")
        normalized.append(group_id)
    if len(normalized) != len(set(normalized)):
        raise CallImportError("Selected group IDs must be unique.")
    return set(normalized)


def _parse_csv(
    data: bytes,
    mode: CallImportMode,
    allowed_groups: set[str],
    max_rows: int,
) -> list[CallImportRecord]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CallImportError("CSV file must use UTF-8 encoding.") from None

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, None)
        if header != list(HEADER):
            raise CallImportError(
                "CSV row 1: header must be exactly date,hour,group_id,calls."
            )
        return _read_rows(
            (
                (row, reader.line_num)
                for row in reader
                if row
            ),
            "CSV",
            mode,
            allowed_groups,
            max_rows,
        )
    except CallImportError:
        raise
    except csv.Error:
        raise CallImportError(
            f"CSV row {max(reader.line_num, 1)}: malformed CSV."
        ) from None


def _read_rows(
    rows: Iterable[tuple[Sequence[object], int]],
    file_format: str,
    mode: CallImportMode,
    allowed_groups: set[str],
    max_rows: int,
) -> list[CallImportRecord]:
    records: list[CallImportRecord] = []
    seen: set[tuple[date, int, str]] = set()
    for row_number, (values, source_row) in enumerate(rows, start=1):
        if row_number > max_rows:
            raise CallImportError(
                f"{file_format} row {source_row}: maximum of {max_rows} data rows exceeded."
            )
        if len(values) != len(HEADER):
            raise CallImportError(
                f"{file_format} row {source_row}: expected exactly four values."
            )
        record = _make_record(values, mode, file_format, source_row)
        if record.group_id not in allowed_groups:
            raise CallImportError(
                f"{file_format} row {source_row}: group_id is not in the selected groups."
            )
        key = (record.date, record.hour, record.group_id)
        if key in seen:
            raise CallImportError(
                f"{file_format} row {source_row}: duplicate date/hour/group row."
            )
        seen.add(key)
        records.append(record)
    if not records:
        raise CallImportError(f"{file_format} file has no data rows.")
    return records


def _make_record(
    values: Sequence[object],
    mode: CallImportMode,
    file_format: str,
    row_number: int,
) -> CallImportRecord:
    raw_date, raw_hour, raw_group_id, raw_calls = values
    if file_format == "CSV":
        date_value = raw_date
        hour = _parse_csv_integer(raw_hour, file_format, row_number, "hour")
        if mode == "history":
            calls: int | float = _parse_csv_integer(
                raw_calls, file_format, row_number, "calls"
            )
        else:
            calls = _parse_csv_float(raw_calls, file_format, row_number)
    else:
        date_value = _parse_xlsx_date(raw_date, file_format, row_number)
        hour = _parse_xlsx_integer(raw_hour, file_format, row_number, "hour")
        if mode == "history":
            calls = _parse_xlsx_integer(raw_calls, file_format, row_number, "calls")
        else:
            calls = _parse_xlsx_float(raw_calls, file_format, row_number)
    if not 0 <= hour <= 22:
        raise CallImportError(
            f"{file_format} row {row_number}: hour must be an integer from 0 through 22."
        )

    payload = {
        "date": date_value,
        "hour": hour,
        "group_id": raw_group_id,
    }
    model = PipelineHistoryObservation if mode == "history" else PipelinePlannedCalls
    payload["calls" if mode == "history" else "calls_per_hour"] = calls
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        errors = error.errors(include_input=False, include_context=False)
        field = errors[0]["loc"][0] if errors and errors[0]["loc"] else "row"
        raise CallImportError(
            f"{file_format} row {row_number}: invalid {field}."
        ) from None


def _parse_csv_integer(
    value: object, file_format: str, row_number: int, field: str
) -> int:
    if not isinstance(value, str) or not _INTEGER.fullmatch(value.strip()):
        raise CallImportError(
            f"{file_format} row {row_number}: invalid {field}."
        )
    try:
        return int(value.strip())
    except ValueError:
        raise CallImportError(
            f"{file_format} row {row_number}: invalid {field}."
        ) from None


def _parse_csv_float(value: object, file_format: str, row_number: int) -> float:
    if not isinstance(value, str):
        raise CallImportError(
            f"{file_format} row {row_number}: invalid calls."
        )
    try:
        calls = float(value.strip())
    except ValueError:
        raise CallImportError(
            f"{file_format} row {row_number}: invalid calls."
        ) from None
    if not math.isfinite(calls):
        raise CallImportError(
            f"{file_format} row {row_number}: invalid calls."
        )
    return calls


def _parse_xlsx_date(
    value: object, file_format: str, row_number: int
) -> date | str:
    if isinstance(value, datetime):
        if value.tzinfo is not None or value.time() != time.min:
            raise CallImportError(
                f"{file_format} row {row_number}: date must not include a time."
            )
        return value.date()
    if isinstance(value, (date, str)):
        return value
    raise CallImportError(f"{file_format} row {row_number}: invalid date.")


def _parse_xlsx_integer(
    value: object, file_format: str, row_number: int, field: str
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CallImportError(
            f"{file_format} row {row_number}: invalid {field}."
        )
    return value


def _parse_xlsx_float(value: object, file_format: str, row_number: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CallImportError(
            f"{file_format} row {row_number}: invalid calls."
        )
    try:
        calls = float(value)
    except OverflowError:
        raise CallImportError(
            f"{file_format} row {row_number}: invalid calls."
        ) from None
    if not math.isfinite(calls):
        raise CallImportError(
            f"{file_format} row {row_number}: invalid calls."
        )
    return calls


def _parse_xlsx(
    data: bytes,
    mode: CallImportMode,
    allowed_groups: set[str],
    max_rows: int,
) -> list[CallImportRecord]:
    if not DEFUSEDXML:
        raise CallImportError("XLSX XML protection is unavailable.")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            max_row = _preflight_archive(archive, infos, max_rows)
            workbook = load_workbook(
                io.BytesIO(data),
                read_only=True,
                data_only=False,
                keep_links=False,
            )
            try:
                if workbook.sheetnames != ["calls"]:
                    raise CallImportError(
                        "XLSX must contain exactly one sheet named calls."
                    )
                worksheet = workbook["calls"]
                worksheet.reset_dimensions()
                rows = []
                for row_number, cells in enumerate(
                    worksheet.iter_rows(
                        min_row=1,
                        max_row=max(max_row, 1),
                        min_col=1,
                        max_col=len(HEADER),
                    ),
                    start=1,
                ):
                    values = [cell.value for cell in cells]
                    if row_number == 1:
                        if values != list(HEADER):
                            raise CallImportError(
                                "XLSX row 1: header must be exactly date,hour,group_id,calls."
                            )
                        continue
                    if not any(value is not None for value in values):
                        continue
                    rows.append((values, row_number))
                    if len(rows) > max_rows:
                        raise CallImportError(
                            f"XLSX row {row_number}: maximum of {max_rows} data rows exceeded."
                        )
                return _read_rows(
                    iter(rows),
                    "XLSX",
                    mode,
                    allowed_groups,
                    max_rows,
                )
            finally:
                workbook.close()
    except CallImportError:
        raise
    except Exception:
        raise CallImportError("XLSX file is invalid or unsafe.") from None


def _preflight_archive(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    max_rows: int,
) -> int:
    if not 1 <= len(infos) <= MAX_XLSX_ENTRIES:
        raise CallImportError("XLSX archive has an unsafe number of entries.")

    names: set[str] = set()
    folded_names: set[str] = set()
    declared_size = 0
    metadata_size = 0
    xml_parts: dict[str, bytes] = {}
    xml_counts = {"elements": 0, "attributes": 0}

    for info in infos:
        name = info.filename
        path = PurePosixPath(name)
        folded = name.casefold()
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or "\x00" in name
            or PureWindowsPath(name).drive
            or ".." in path.parts
            or name in names
            or folded in folded_names
        ):
            raise CallImportError("XLSX archive contains an unsafe or duplicate path.")
        names.add(name)
        folded_names.add(folded)
        if info.flag_bits & 0x1:
            raise CallImportError("Encrypted XLSX entries are not accepted.")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise CallImportError("XLSX archive uses an unsupported compression method.")
        if info.file_size > MAX_XLSX_ENTRY_BYTES:
            raise CallImportError("XLSX archive entry exceeds the size limit.")
        declared_size += info.file_size
        if declared_size > MAX_XLSX_UNCOMPRESSED_BYTES:
            raise CallImportError("XLSX archive exceeds the uncompressed size limit.")
        if info.file_size and (
            not info.compress_size
            or info.file_size > info.compress_size * MAX_XLSX_COMPRESSION_RATIO
        ):
            raise CallImportError("XLSX archive has an unsafe compression ratio.")
        if folded.startswith("xl/externallinks/"):
            raise CallImportError("XLSX external links are not accepted.")
        if folded.endswith("vbaproject.bin"):
            raise CallImportError("XLSX macro content is not accepted.")
        is_xml = folded.endswith((".xml", ".rels"))
        is_metadata = (
            name in ("[Content_Types].xml", "xl/workbook.xml")
            or name.lower().endswith(".rels")
        )
        if is_metadata:
            metadata_size += info.file_size
            if (
                info.file_size > MAX_XLSX_METADATA_BYTES
                or metadata_size > MAX_XLSX_METADATA_BYTES
            ):
                raise CallImportError("XLSX package metadata exceeds the size limit.")
        if is_xml:
            if info.file_size > MAX_XLSX_XML_PART_BYTES:
                raise CallImportError("XLSX XML part exceeds the size limit.")
            content = _read_bounded_entry(
                archive,
                info,
                max_bytes=MAX_XLSX_XML_PART_BYTES,
                retain=True,
            )
            _preflight_xml_part(name, content, xml_counts)
            if is_metadata:
                xml_parts[name] = content
        else:
            _read_bounded_entry(archive, info)

    worksheet_part, shared_strings_part = _validate_package_xml(xml_parts)
    worksheet_parts = {
        name.casefold()
        for name in names
        if name.casefold().startswith("xl/worksheets/")
        and not name.endswith("/")
        and "/_rels/" not in name.casefold()
    }
    if worksheet_parts != {worksheet_part.casefold()}:
        raise CallImportError("XLSX must contain exactly one worksheet.")
    if shared_strings_part is None:
        if "xl/sharedstrings.xml" in folded_names:
            raise CallImportError("XLSX shared strings relationship is invalid.")
    else:
        if shared_strings_part not in names:
            raise CallImportError("XLSX shared strings part is missing.")
        _preflight_shared_strings(archive, shared_strings_part)
    return _preflight_worksheet(archive, worksheet_part, max_rows)


# ponytail: ZipInfo caps yielded data but does not verify raw DEFLATE size;
# add bounded zlib validation if independent stream verification is required.
def _read_bounded_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    max_bytes: int = MAX_XLSX_ENTRY_BYTES,
    retain: bool = False,
) -> bytes:
    content = bytearray() if retain else None
    actual_size = 0
    with archive.open(info) as entry:
        while chunk := entry.read(64 * 1024):
            actual_size += len(chunk)
            if (
                actual_size > info.file_size
                or actual_size > max_bytes
                or actual_size > MAX_XLSX_UNCOMPRESSED_BYTES
            ):
                raise CallImportError("XLSX archive entry exceeds the size limit.")
            if content is not None:
                content.extend(chunk)
    if actual_size != info.file_size:
        raise CallImportError("XLSX archive entry has an invalid size.")
    return bytes(content) if content is not None else b""


def _preflight_xml_part(
    name: str,
    content: bytes,
    totals: dict[str, int],
) -> None:
    _preflight_xml_bytes(content)
    folded_name = name.casefold()
    is_shared_strings = folded_name == "xl/sharedstrings.xml"
    is_worksheet = (
        folded_name.startswith("xl/worksheets/")
        and "/_rels/" not in folded_name
    )
    if is_shared_strings:
        element_limit = MAX_XLSX_SHARED_STRING_XML_ELEMENTS
    elif is_worksheet:
        element_limit = MAX_XLSX_WORKSHEET_XML_ELEMENTS
    else:
        element_limit = MAX_XLSX_XML_ELEMENTS_PER_EAGER_PART

    element_count = 0
    depth = 0
    pending_namespaces = 0
    try:
        for event, element in iterparse(
            io.BytesIO(content),
            events=("start", "end", "start-ns"),
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        ):
            if event == "start-ns":
                pending_namespaces += 1
                if pending_namespaces > MAX_XLSX_XML_ATTRIBUTES_PER_ELEMENT:
                    raise CallImportError(
                        "XLSX XML element has too many attributes."
                    )
            elif event == "start":
                depth += 1
                element_count += 1
                current_attributes = len(element.attrib) + pending_namespaces
                pending_namespaces = 0
                totals["elements"] += 1
                totals["attributes"] += current_attributes
                if depth > MAX_XLSX_XML_DEPTH:
                    raise CallImportError("XLSX XML exceeds the depth limit.")
                if current_attributes > MAX_XLSX_XML_ATTRIBUTES_PER_ELEMENT:
                    raise CallImportError(
                        "XLSX XML element has too many attributes."
                    )
                if totals["attributes"] > MAX_XLSX_XML_ATTRIBUTES:
                    raise CallImportError(
                        "XLSX XML attribute count exceeds the limit."
                    )
                if totals["elements"] > MAX_XLSX_XML_ELEMENTS:
                    raise CallImportError(
                        "XLSX XML element count exceeds the limit."
                    )
                if element_count > element_limit:
                    if folded_name == "xl/styles.xml":
                        raise CallImportError(
                            "XLSX styles XML exceeds the element limit."
                        )
                    if is_shared_strings:
                        raise CallImportError(
                            "XLSX shared strings XML exceeds the element limit."
                        )
                    raise CallImportError(
                        "XLSX XML part exceeds the element limit."
                    )
                if is_shared_strings and element.tag.rsplit("}", 1)[-1] == "r":
                    raise CallImportError(
                        "XLSX shared strings rich text is not accepted."
                    )
            else:
                element.clear()
                depth -= 1
    except CallImportError:
        raise
    except Exception:
        raise CallImportError("XLSX XML part is invalid or unsafe.") from None


def _preflight_xml_bytes(content: bytes) -> None:
    if b"\x00" in content:
        raise CallImportError("XLSX XML must be UTF-8 without NUL bytes.")
    try:
        content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CallImportError("XLSX XML must be UTF-8.") from None

    xml_bytes = content[3:] if content.startswith(b"\xef\xbb\xbf") else content
    declaration = _XML_DECLARATION.match(xml_bytes)
    if declaration is not None:
        encoding = _XML_ENCODING.search(declaration.group(1))
        if encoding is not None and encoding.group(2).lower() != b"utf-8":
            raise CallImportError("XLSX XML declaration must specify UTF-8.")

    if content.count(b"xmlns") > MAX_XLSX_XML_NAMESPACE_MARKERS:
        raise CallImportError("XLSX XML namespace marker count exceeds the limit.")

    _preflight_xml_markup_tokens(content)


def _preflight_xml_markup_tokens(content: bytes) -> None:
    position = 0
    while (start := content.find(b"<", position)) >= 0:
        if content.startswith(b"<!--", start):
            end = content.find(b"-->", start + 4)
            terminator_bytes = 3
        elif content.startswith(b"<![CDATA[", start):
            end = content.find(b"]]>", start + 9)
            terminator_bytes = 3
        elif content.startswith(b"<?", start):
            end = content.find(b"?>", start + 2)
            terminator_bytes = 2
        else:
            quote = None
            end = start + 1
            while end < len(content):
                byte = content[end]
                if quote is not None:
                    if byte == quote:
                        quote = None
                elif byte in (ord('"'), ord("'")):
                    quote = byte
                elif byte == ord(">"):
                    break
                end += 1
            if end == len(content):
                if end - start > MAX_XLSX_XML_MARKUP_TOKEN_BYTES:
                    raise CallImportError(
                        "XLSX XML markup token exceeds the size limit."
                    )
                return
            terminator_bytes = 1

        if end < 0:
            if len(content) - start > MAX_XLSX_XML_MARKUP_TOKEN_BYTES:
                raise CallImportError(
                    "XLSX XML markup token exceeds the size limit."
                )
            return
        token_end = end + terminator_bytes
        if token_end - start > MAX_XLSX_XML_MARKUP_TOKEN_BYTES:
            raise CallImportError("XLSX XML markup token exceeds the size limit.")
        position = token_end


def _validate_package_xml(xml_parts: dict[str, bytes]) -> tuple[str, str | None]:
    content_types = xml_parts.get("[Content_Types].xml")
    workbook_xml = xml_parts.get("xl/workbook.xml")
    relationships_xml = xml_parts.get("xl/_rels/workbook.xml.rels")
    if content_types is None or workbook_xml is None or relationships_xml is None:
        raise CallImportError("XLSX package is missing required metadata.")
    try:
        root = fromstring(
            content_types,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
        workbook_parts = []
        shared_strings_parts = []
        for element in root.iter():
            content_type = element.attrib.get("ContentType", "")
            part_name = element.attrib.get("PartName", "")
            normalized_type = (content_type + part_name).casefold()
            if "macro" in normalized_type or "vbaproject" in normalized_type:
                raise CallImportError("XLSX macro content is not accepted.")
            if content_type == _XLSX_WORKBOOK_CONTENT_TYPE:
                workbook_parts.append(part_name)
            elif content_type == _XLSX_SHARED_STRINGS_CONTENT_TYPE:
                shared_strings_parts.append(part_name)
        if workbook_parts != ["/xl/workbook.xml"]:
            raise CallImportError("Only standard XLSX workbooks are accepted.")
        if shared_strings_parts not in ([], ["/xl/sharedStrings.xml"]):
            raise CallImportError("XLSX shared strings manifest is invalid.")
        for name, content in xml_parts.items():
            if name.lower().endswith(".rels"):
                relationships = fromstring(
                    content,
                    forbid_dtd=True,
                    forbid_entities=True,
                    forbid_external=True,
                )
                if any(
                    element.attrib.get("TargetMode", "").casefold() == "external"
                    for element in relationships.iter()
                ):
                    raise CallImportError("XLSX external links are not accepted.")

        workbook = fromstring(
            workbook_xml,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
        sheets = [
            element
            for element in workbook.iter()
            if element.tag.rsplit("}", 1)[-1] == "sheet"
        ]
        if len(sheets) != 1:
            raise CallImportError("XLSX must contain exactly one worksheet.")
        worksheet_relationship_id = sheets[0].attrib.get(_RELATIONSHIPS_ID)
        if not worksheet_relationship_id:
            raise CallImportError("XLSX worksheet relationship is invalid.")

        relationships = fromstring(
            relationships_xml,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
        relationship_ids: set[str] = set()
        worksheet_relationships = []
        shared_strings_relationships = []
        for element in relationships.iter():
            if element.tag.rsplit("}", 1)[-1] != "Relationship":
                continue
            relationship_id = element.attrib.get("Id", "")
            if not relationship_id or relationship_id in relationship_ids:
                raise CallImportError("XLSX package relationship is invalid.")
            relationship_ids.add(relationship_id)
            relationship_type = element.attrib.get("Type", "").rsplit("/", 1)[-1]
            if relationship_type.casefold() == "worksheet":
                worksheet_relationships.append(element)
            elif relationship_type.casefold() == "sharedstrings":
                shared_strings_relationships.append(element)
        if len(worksheet_relationships) != 1:
            raise CallImportError("XLSX must contain exactly one worksheet.")
        worksheet_relationship = worksheet_relationships[0]
        if worksheet_relationship.attrib.get("Id") != worksheet_relationship_id:
            raise CallImportError("XLSX worksheet relationship is invalid.")
        worksheet_part = _resolve_workbook_target(
            worksheet_relationship.attrib.get("Target", ""),
            expected_directory="xl/worksheets",
        )
        if len(shared_strings_relationships) > 1:
            raise CallImportError("XLSX shared strings relationship is invalid.")
        shared_strings_part = None
        if shared_strings_relationships:
            shared_strings_part = _resolve_workbook_target(
                shared_strings_relationships[0].attrib.get("Target", ""),
                expected_directory="xl",
            )
            if shared_strings_part != "xl/sharedStrings.xml":
                raise CallImportError("XLSX shared strings relationship is invalid.")
        if bool(shared_strings_part) != bool(shared_strings_parts):
            raise CallImportError("XLSX shared strings manifest is invalid.")
        return worksheet_part, shared_strings_part
    except CallImportError:
        raise
    except Exception:
        raise CallImportError("XLSX package XML is invalid or unsafe.") from None


def _resolve_workbook_target(target: str, *, expected_directory: str) -> str:
    if (
        not target
        or "\\" in target
        or "\x00" in target
        or "%" in target
        or "#" in target
        or "?" in target
        or PureWindowsPath(target).drive
        or target.startswith("//")
    ):
        raise CallImportError("XLSX package relationship target is unsafe.")
    package_path = target[1:] if target.startswith("/") else f"xl/{target}"
    parts = package_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise CallImportError("XLSX package relationship target is unsafe.")
    path = PurePosixPath(package_path)
    if path.parent.as_posix() != expected_directory or path.suffix.lower() != ".xml":
        raise CallImportError("XLSX package relationship target is unsupported.")
    return package_path


def _preflight_shared_strings(
    archive: zipfile.ZipFile,
    shared_strings_part: str,
) -> None:
    item_count = 0
    character_count = 0
    try:
        with archive.open(shared_strings_part) as source:
            for _, element in iterparse(
                source,
                events=("end",),
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            ):
                name = element.tag.rsplit("}", 1)[-1]
                if name == "si":
                    item_count += 1
                    if item_count > MAX_XLSX_SHARED_STRINGS:
                        raise CallImportError(
                            "XLSX shared strings exceed the count limit."
                        )
                elif name == "t":
                    character_count += len(element.text or "")
                    if character_count > MAX_XLSX_SHARED_STRING_CHARS:
                        raise CallImportError(
                            "XLSX shared strings exceed the character limit."
                        )
                element.clear()
    except CallImportError:
        raise
    except Exception:
        raise CallImportError(
            "XLSX shared strings XML is invalid or unsafe."
        ) from None


def _preflight_worksheet(
    archive: zipfile.ZipFile,
    worksheet_part: str,
    max_rows: int,
) -> int:
    row_count = 0
    cell_count = 0
    max_row = 0
    try:
        with archive.open(worksheet_part) as source:
            for _, element in iterparse(
                source,
                events=("end",),
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            ):
                name = element.tag.rsplit("}", 1)[-1]
                if name == "f":
                    raise CallImportError("XLSX formula cells are not accepted.")
                if name == "row":
                    row_index = element.attrib.get("r", "")
                    if not row_index.isascii() or not row_index.isdigit():
                        raise CallImportError("XLSX worksheet has an invalid row.")
                    row_number = int(row_index)
                    if row_number < 1:
                        raise CallImportError("XLSX worksheet has an invalid row.")
                    row_count += 1
                    if row_number > max_rows + 1 or row_count > max_rows + 1:
                        raise CallImportError(
                            f"XLSX row {row_number}: maximum of {max_rows} data rows exceeded."
                        )
                    max_row = max(max_row, row_number)
                elif name == "c":
                    reference = _CELL_REFERENCE.fullmatch(element.attrib.get("r", ""))
                    if reference is None:
                        raise CallImportError("XLSX worksheet has an invalid cell.")
                    column = 0
                    for character in reference.group(1):
                        column = column * 26 + ord(character) - ord("A") + 1
                    row_number = int(reference.group(2))
                    if row_number > max_rows + 1:
                        raise CallImportError(
                            f"XLSX row {row_number}: maximum of {max_rows} data rows exceeded."
                        )
                    cell_count += 1
                    if cell_count > (max_rows + 1) * len(HEADER):
                        raise CallImportError("XLSX worksheet has too many cells.")
                    if column > len(HEADER):
                        raise CallImportError(
                            "XLSX rows must contain exactly the four expected columns."
                        )
                    max_row = max(max_row, row_number)
                element.clear()
    except CallImportError:
        raise
    except Exception:
        raise CallImportError("XLSX worksheet XML is invalid or unsafe.") from None
    return max_row
