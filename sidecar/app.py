"""
app.py — combined pipeline (originally: vocab, normalize, readers, extract,
compare, classify, nlp, cases, pipeline).

Load order (each section only depends on sections above it):
  1. vocab       label -> target field vocabulary
  2. normalize   raw value -> comparable Norm
  3. readers     attachment bytes -> Doc (title, entries, text)
  4. extract     Doc -> identified type + extracted/validated fields
  5. compare     SI fields vs BL fields -> match/mismatch
  6. classify    email -> category (rule-based)
  7. nlp         optional Laya model classification, hybrid with classify()
  8. cases       group emails into cases, plan DB rows
  9. pipeline    process_email() / to_submission_entry() entry points

Public entry points: process_email(email, read_bytes), sort_into_cases(emails, results).
"""
import io
import os
import re
import uuid
import zipfile
import xml.etree.ElementTree as ET

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional


# ======================================================================
# 1. vocab — which document label means which of the 7 target fields
# ======================================================================

FIELDS = (
    "shipper",
    "consignee",
    "notify_party",
    "port_of_loading",
    "port_of_discharge",
    "container_count",
    "gross_weight_kg",
)

LABEL_TO_FIELD = {
    "shipper": "shipper",
    "shipper/exporter": "shipper",

    "consignee": "consignee",
    "to the order of": "consignee",

    "notify": "notify_party",
    "notify party": "notify_party",
    "notify party/intermediate consignee": "notify_party",

    "port of loading": "port_of_loading",
    "pol": "port_of_loading",
    "load port": "port_of_loading",

    "port of discharge": "port_of_discharge",
    "pod": "port_of_discharge",
    "discharge port": "port_of_discharge",

    "no. of containers": "container_count",
    "no. of containers or packages": "container_count",
    "total containers": "container_count",
    "container count": "container_count",

    "gross weight": "gross_weight_kg",
    "gross wt": "gross_weight_kg",
    "total gross weight": "gross_weight_kg",
    "total gross wt": "gross_weight_kg",
}

OTHER_LABELS = {
    "vessel",
    "vessel name",
    "ocean vessel",
    "export carrier",
    "voyage",
    "voy.",
    "voy. no",
    "voyage no.",
    "booking ref",
    "booking reference",
    "booking no.",
    "booking no",
    "description",
    "description of goods",
    "commodity",
    "kinds of packages; description of goods",
    "hs code",
    "freight",
    "oc no.",
    "oc no",
    "net weight",
    "container no.",
    "container no",
    "bill of lading no.",
    "b/l no.",
    "bl no.",
    "b/l number",
    "order no.",
}


def norm_label(text: str) -> str:
    s = re.sub(r"[^\x00-\x7f]+", " ", text or "")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s.strip(":;, ")


def field_for_label(text: str):
    return LABEL_TO_FIELD.get(norm_label(text))


def is_other_label(text: str) -> bool:
    return norm_label(text) in OTHER_LABELS


_FUZZY = [
    ("notify_party", re.compile(r"notify", re.I)),
    ("consignee", re.compile(r"consign|to the order", re.I)),
    ("shipper", re.compile(r"shipper|exporter|seller", re.I)),
    (
        "port_of_loading",
        re.compile(r"\bpol\b|loading\s+port|port of load", re.I),
    ),
    (
        "port_of_discharge",
        re.compile(r"\bpod\b|discharge|port of dest", re.I),
    ),
    (
        "container_count",
        re.compile(r"container(?!\s*no)|no\.\s*of\s+(cont|pack)", re.I),
    ),
    (
        "gross_weight_kg",
        re.compile(r"gross\s*(w|weight)", re.I),
    ),
]


def fuzzy_field_for_label(text: str):
    cleaned = re.sub(r"[^\x00-\x7f]+", " ", text or "")

    for field_name, regex in _FUZZY:
        if regex.search(cleaned):
            return field_name

    return None
# ======================================================================
# 2. normalize — raw field text -> comparable Norm
# ======================================================================

@dataclass
class Norm:
    display: str
    key: Any
    numeric: Optional[float] = None


_ABBREV = {
    "CORPORATION": "CORP",
    "INCORPORATED": "INC",
    "LIMITED": "LTD",
    "COMPANY": "CO",
    "COMPANIES": "CO",
}


def norm_text(text: str) -> str:
    s = (text or "").upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return " ".join(_ABBREV.get(tok, tok) for tok in s.split())


def norm_party(lines) -> Norm:
    text = norm_text(" ".join(lines or []))
    return Norm(display=text, key={"text": text})


_LOCODE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{3})\s*$")


def norm_port(lines) -> Norm:
    raw = " ".join(lines or []).strip()
    m = _LOCODE.search(raw)
    code = m.group(1) if m else None
    name = norm_text(_LOCODE.sub("", raw))
    display = name + (f" [{code}]" if code else "")
    return Norm(
        display=display,
        key={"name": name, "code": code},
    )


def norm_containers(lines) -> Norm:
    raw = " ".join(lines or []).strip()

    m = re.match(
        r"\s*(\d+)\s*(?:x|X|\*)?\s*(.*)$",
        raw,
    )

    if not m:
        return Norm(
            display=raw.upper(),
            key={"count": None, "type": None},
        )

    count = int(m.group(1))
    ctype = re.sub(
        r"[^A-Z0-9]",
        "",
        m.group(2).upper(),
    ) or None

    display = f"{count}" + (f" x {ctype}" if ctype else "")

    return Norm(
        display=display,
        key={"count": count, "type": ctype},
        numeric=float(count),
    )


_TO_KG = {
    "KG": 1.0,
    "KGS": 1.0,
    "MT": 1000.0,
    "MTS": 1000.0,
    "TON": 1000.0,
    "TONS": 1000.0,
    "TONNE": 1000.0,
    "TONNES": 1000.0,
    "LB": 0.45359237,
    "LBS": 0.45359237,
}


def norm_weight(lines) -> Norm:
    raw = " ".join(lines or []).strip()

    m = re.search(
        r"(\d[\d,]*(?:\.\d+)?)\s*([A-Za-z]+)?",
        raw,
    )

    if not m:
        return Norm(
            display=raw.upper(),
            key={"kg": None},
        )

    value = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "KG").upper()

    kg = round(
        value * _TO_KG.get(unit, 1.0),
        3,
    )

    shown = int(kg) if float(kg).is_integer() else kg

    return Norm(
        display=f"{shown} kg",
        key={"kg": kg},
        numeric=kg,
    )


NORMALIZERS = {
    "shipper": norm_party,
    "consignee": norm_party,
    "notify_party": norm_party,
    "port_of_loading": norm_port,
    "port_of_discharge": norm_port,
    "container_count": norm_containers,
    "gross_weight_kg": norm_weight,
}


def normalize_value(field_name: str, lines) -> Norm:
    return NORMALIZERS[field_name](lines)


# ======================================================================
# 3. readers — attachment bytes -> Doc
# ======================================================================

@dataclass
class Entry:
    label: str
    lines: List[str] = field(default_factory=list)


@dataclass
class Doc:
    kind: str
    name: str = ""
    title: str = ""
    entries: List[Entry] = field(default_factory=list)
    text: str = ""
    error: Optional[str] = None
    meta: dict = field(default_factory=dict)


_TITLE_RX = re.compile(
    r"instruction|bill of lading|b/l\b|commercial invoice|"
    r"packing list|certificate of origin",
    re.I,
)

_KV_RX = re.compile(r"^([^:]{1,80}?)\s*:\s*(.*)$")


def _read_txt(data: bytes, name: str) -> Doc:
    text = data.decode("utf-8", errors="replace")
    raw_lines = text.splitlines()

    doc = Doc(
        kind="txt",
        name=name,
        text=text,
    )

    body_start = 0

    for i, ln in enumerate(raw_lines):
        if ln.strip():
            doc.title = ln.strip()
            body_start = i + 1
            break

    current = None

    for ln in raw_lines[body_start:]:
        stripped = ln.strip()

        if not stripped or set(stripped) <= set("=-"):
            continue

        if ln[:1] in (" ", "\t"):
            if current is not None:
                current.lines.append(stripped)
            continue

        m = _KV_RX.match(ln)

        if m:
            value = m.group(2).strip()

            current = Entry(
                m.group(1).strip(),
                [value] if value else [],
            )

            doc.entries.append(current)
        else:
            current = None

    return doc


_SS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _fmt_num(val: str) -> str:
    try:
        f = float(val)
        return str(int(f)) if f.is_integer() else val
    except ValueError:
        return val


def _read_xlsx(data: bytes, name: str) -> Doc:
    doc = Doc(
        kind="xlsx",
        name=name,
    )

    z = zipfile.ZipFile(io.BytesIO(data))

    shared = []

    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(
            z.read("xl/sharedStrings.xml")
        )

        shared = [
            "".join(t.text or "" for t in si.iter(_SS + "t"))
            for si in root.findall(_SS + "si")
        ]

    sheets = sorted(
        n
        for n in z.namelist()
        if re.match(r"xl/worksheets/sheet\d+\.xml$", n)
    )

    rows = []

    for sheet in sheets:
        root = ET.fromstring(z.read(sheet))

        for row in root.iter(_SS + "row"):
            cells = []

            for c in row.findall(_SS + "c"):
                t = c.get("t")
                v = c.find(_SS + "v")

                if t == "s" and v is not None:
                    val = shared[int(v.text)]

                elif t == "inlineStr":
                    val = "".join(
                        x.text or ""
                        for x in c.iter(_SS + "t")
                    )

                elif v is not None:
                    val = (
                        _fmt_num(v.text or "")
                        if t in (None, "n")
                        else (v.text or "")
                    )

                else:
                    val = ""

                if val.strip():
                    cells.append(val.strip())

            if cells:
                rows.append(cells)

    doc.text = "\n".join(
        " | ".join(r)
        for r in rows
    )

    title_idx = next(
        (
            i
            for i, r in enumerate(rows)
            if any(_TITLE_RX.search(c) for c in r)
        ),
        None,
    )

    if title_idx is None:
        doc.error = "no_title_found"
        return doc

    doc.title = next(
        c
        for c in rows[title_idx]
        if _TITLE_RX.search(c)
    )

    doc.meta["title_row"] = rows[title_idx]

    for r in rows[title_idx + 1:]:
        if len(r) >= 2:
            lines = [
                p.strip()
                for p in r[1].split(" | ")
                if p.strip()
            ]
            doc.entries.append(
                Entry(r[0], lines)
            )
        else:
            doc.entries.append(
                Entry(r[0], [])
            )

    return doc


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _para_text(p) -> str:
    out = []

    for el in p.iter():
        if el.tag == _W + "t":
            out.append(el.text or "")
        elif el.tag == _W + "br":
            out.append("\n")
        elif el.tag == _W + "tab":
            out.append(" ")

    return "".join(out)


def _read_docx(data: bytes, name: str) -> Doc:
    doc = Doc(
        kind="docx",
        name=name,
    )

    z = zipfile.ZipFile(io.BytesIO(data))

    root = ET.fromstring(
        z.read("word/document.xml")
    )

    body = root.find(_W + "body")

    text_lines = []

    for child in body:
        if child.tag == _W + "p":
            text = _para_text(child).strip()

            if not text:
                continue

            text_lines.append(text)

            if not doc.title:
                doc.title = text
                continue

            m = _KV_RX.match(text)

            if m:
                value = m.group(2).strip()

                doc.entries.append(
                    Entry(
                        m.group(1).strip(),
                        [value] if value else [],
                    )
                )

        elif child.tag == _W + "tbl":
            for tr in child.iter(_W + "tr"):
                cells = []

                for tc in tr.findall(_W + "tc"):
                    cells.append(
                        "\n".join(
                            _para_text(p)
                            for p in tc.findall(_W + "p")
                        )
                    )

                if not cells:
                    continue

                label = cells[0].strip()

                lines = [
                    ln.strip()
                    for ln in (
                        cells[1]
                        if len(cells) > 1
                        else ""
                    ).split("\n")
                    if ln.strip()
                ]

                text_lines.append(
                    label + ": " + " | ".join(lines)
                )

                doc.entries.append(
                    Entry(label, lines)
                )

    doc.text = "\n".join(text_lines)

    return doc


_CONTAINER_RX = re.compile(r"^[A-Z]{4}\d{7}$")
_WEIGHT_RX = re.compile(r"^\d{1,3}(,\d{3})*$")


def _pdf_text(data: bytes):
    try:
        import pypdf
    except ImportError:
        raise RuntimeError(
            "Reading PDFs needs pypdf. "
            "Install it with: pip install pypdf"
        )

    try:
        reader = pypdf.PdfReader(
            io.BytesIO(data)
        )

        text = "\n".join(
            (p.extract_text() or "")
            for p in reader.pages
        )

        has_images = any(
            len(p.images)
            for p in reader.pages
        )

    except Exception:
        text = ""
        has_images = None

        try:
            import pdfplumber

            with pdfplumber.open(
                io.BytesIO(data)
            ) as pdf:
                text = "\n".join(
                    (p.extract_text() or "")
                    for p in pdf.pages
                )

        except Exception:
            return "", "corrupt_file"

    if text.strip():
        return text, None

    return "", (
        "image_only_scan"
        if has_images
        else "no_text"
    )


def _read_pdf(data: bytes, name: str) -> Doc:
    doc = Doc(
        kind="pdf",
        name=name,
    )

    text, problem = _pdf_text(data)

    if problem:
        doc.error = problem
        return doc

    doc.text = text

    lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip()
    ]

    if not lines:
        doc.error = "no_text"
        return doc

    doc.title = lines[0]

    current = None
    in_table = False
    weights = []

    i = 1

    while i < len(lines):
        ln = lines[i]

        if in_table:
            if (
                _CONTAINER_RX.match(ln)
                and i + 2 < len(lines)
            ):
                if _WEIGHT_RX.match(lines[i + 2]):
                    weights.append(
                        int(
                            lines[i + 2].replace(",", "")
                        )
                    )

                i += 3
                continue

            if ":" in ln or ln.upper().startswith("HS CODE"):
                in_table = False
            else:
                i += 1
                continue

        if ln.upper().startswith("CONTAINER NO"):
            in_table = True
            current = None
            i += 1
            continue

        m = _KV_RX.match(ln)

        label_only = (
            field_for_label(ln)
            or is_other_label(ln)
        )

        if label_only and ":" not in ln:
            current = Entry(ln)
            doc.entries.append(current)

        elif (
            m
            and (
                field_for_label(m.group(1))
                or is_other_label(m.group(1))
                or fuzzy_field_for_label(m.group(1))
            )
        ):
            value = m.group(2).strip()

            current = Entry(
                m.group(1).strip(),
                [value] if value else [],
            )

            doc.entries.append(current)

        elif current is not None:
            current.lines.append(ln)

        i += 1

    if weights:
        doc.meta["container_weights"] = weights

    return doc


_READERS = {
    ".txt": _read_txt,
    ".xlsx": _read_xlsx,
    ".docx": _read_docx,
    ".pdf": _read_pdf,
}


def read_document_bytes(data: bytes, filename: str) -> Doc:
    suffix = Path(filename).suffix.lower()
    reader = _READERS.get(suffix)

    if reader is None:
        return Doc(
            kind=suffix.lstrip("."),
            name=filename,
            error="unsupported_format",
        )

    try:
        return reader(data, filename)

    except (
        zipfile.BadZipFile,
        ET.ParseError,
        KeyError,
    ):
        return Doc(
            kind=suffix.lstrip("."),
            name=filename,
            error="corrupt_file",
        )


def read_document(path) -> Doc:
    path = Path(path)
    return read_document_bytes(
        path.read_bytes(),
        path.name,
    )


# ======================================================================
# 4. extract — Doc -> identified type + extracted/validated fields
# ======================================================================

_TITLE_RULES = [
    (
        "SI",
        re.compile(
            r"shipping instruction|"
            r"(bill of lading|b/l|\bbl)\s+instruction",
            re.I,
        ),
    ),
    (
        "BL",
        re.compile(
            r"bill of lading|\bb/l\b",
            re.I,
        ),
    ),
    (
        "INVOICE",
        re.compile(r"invoice", re.I),
    ),
    (
        "OTHER",
        re.compile(
            r"packing list|certificate of origin",
            re.I,
        ),
    ),
]

_PLACEHOLDER = re.compile(
    r"^[\s_\-?.]*$|"
    r"^(n/?a|nil|tbc|tba|tbd|none|-+)$",
    re.I,
)

_UNIT_ONLY = re.compile(
    r"^[\s_\-?.]*\s*(mt|mts|kg|kgs)?\s*$",
    re.I,
)


def identify_document_type(doc: Doc) -> dict:
    if doc.error:
        return {
            "doc_type": "UNKNOWN",
            "confidence": 0.0,
            "evidence": [
                f"unreadable: {doc.error}"
            ],
        }

    for dtype, rx in _TITLE_RULES:
        if rx.search(doc.title):
            return {
                "doc_type": dtype,
                "confidence": 0.98,
                "evidence": [
                    f"title: {doc.title!r}"
                ],
            }

    labels = {
        norm_label(e.label)
        for e in doc.entries
    }

    if labels & {
        "bill of lading no.",
        "b/l no.",
        "bl no.",
        "b/l number",
    }:
        return {
            "doc_type": "BL",
            "confidence": 0.6,
            "evidence": [
                "has a B/L number field"
            ],
        }

    return {
        "doc_type": "UNKNOWN",
        "confidence": 0.3,
        "evidence": [
            f"no recognisable title: {doc.title!r}"
        ],
    }


def _is_blank(field_name: str, value: str) -> bool:
    v = (value or "").strip()

    if field_name == "gross_weight_kg":
        return (
            bool(_UNIT_ONLY.match(v))
            or bool(_PLACEHOLDER.match(v))
        )

    return bool(_PLACEHOLDER.match(v))


def extract_fields(
    doc: Doc,
    strategy: str = "strict",
) -> dict:
    mapper = field_for_label
    confidence = 0.99

    if strategy == "relaxed":
        confidence = 0.8
        mapper = (
            lambda label:
            field_for_label(label)
            or fuzzy_field_for_label(label)
        )

    out = {
        f: {
            "present": False,
            "blank": True,
            "label": None,
            "lines": [],
            "value": "",
            "full": "",
            "confidence": 0.0,
        }
        for f in FIELDS
    }

    for entry in doc.entries:
        f = mapper(entry.label)

        if f is None or out[f]["present"]:
            continue

        value = (
            entry.lines[0]
            if entry.lines
            else ""
        )

        out[f].update(
            present=True,
            label=entry.label,
            lines=list(entry.lines),
            value=value,
            full=" | ".join(entry.lines),
            blank=_is_blank(f, value),
            confidence=(
                confidence
                if not _is_blank(f, value)
                else 0.0
            ),
        )

    return out


def _first_number(text: str) -> Optional[float]:
    m = re.search(
        r"\d[\d,]*(?:\.\d+)?",
        text or "",
    )

    return (
        float(m.group(0).replace(",", ""))
        if m
        else None
    )


def validate_extraction(fields: dict) -> dict:
    issues = []

    for f in FIELDS:
        item = fields[f]

        if not item["present"]:
            issues.append({
                "field": f,
                "problem": "missing",
            })

        elif item["blank"]:
            issues.append({
                "field": f,
                "problem": "blank",
            })

        elif (
            f == "container_count"
            and not re.search(r"\d", item["value"])
        ):
            issues.append({
                "field": f,
                "problem": "unparseable",
            })

        elif (
            f == "gross_weight_kg"
            and not (
                (_first_number(item["value"]) or 0) > 0
            )
        ):
            issues.append({
                "field": f,
                "problem": "unparseable",
            })

    return {
        "status": (
            "VALID"
            if not issues
            else "INVALID"
        ),
        "issues": issues,
    }


def extract_with_retry(
    doc: Doc,
    max_retries: int = 2,
    llm: Optional[Callable[[Doc], dict]] = None,
) -> dict:
    ladder = [
        "strict",
        "relaxed",
        "llm",
    ][:1 + max_retries]

    attempts = []
    best = None

    for n, strategy in enumerate(ladder, start=1):
        if strategy == "llm":
            if llm is None:
                attempts.append({
                    "attempt": n,
                    "strategy": "llm",
                    "status": "SKIPPED",
                    "issues": [],
                    "note": "no LLM fallback configured",
                })
                continue

            fields = llm(doc)

        else:
            fields = extract_fields(
                doc,
                strategy,
            )

        verdict = validate_extraction(fields)

        attempts.append({
            "attempt": n,
            "strategy": strategy,
            "status": verdict["status"],
            "issues": verdict["issues"],
        })

        if (
            best is None
            or len(verdict["issues"])
            < len(best[1]["issues"])
        ):
            best = (fields, verdict)

        if verdict["status"] == "VALID":
            break

    if best is None:
        return {
            "fields": {
                f: {
                    "present": False,
                    "blank": True,
                    "label": None,
                    "lines": [],
                    "value": "",
                    "full": "",
                    "confidence": 0.0,
                }
                for f in FIELDS
            },
            "validation": {
                "status": "INVALID",
                "issues": [
                    {
                        "field": f,
                        "problem": "missing",
                    }
                    for f in FIELDS
                ],
            },
            "attempts": attempts,
        }

    fields, verdict = best

    return {
        "fields": fields,
        "validation": verdict,
        "attempts": attempts,
    }


def process_document(
    doc: Doc,
    llm: Optional[Callable[[Doc], dict]] = None,
) -> dict:
    ident = identify_document_type(doc)

    result = {
        "file": doc.name,
        "kind": doc.kind,
        **ident,
        "state": "COMPLETED",
        "review_reason": None,
        "fields": None,
        "validation": None,
        "attempts": [],
    }

    if doc.error:
        result.update(
            state="REQUIRES_REVIEW",
            review_reason="unreadable",
            error=doc.error,
        )
        return result

    if ident["doc_type"] not in ("SI", "BL"):
        return result

    extracted = extract_with_retry(
        doc,
        llm=llm,
    )

    result.update(
        fields=extracted["fields"],
        validation=extracted["validation"],
        attempts=extracted["attempts"],
    )

    if extracted["validation"]["status"] != "VALID":
        result.update(
            state="REQUIRES_REVIEW",
            review_reason="missing_value",
        )

    return result


# ======================================================================
# 5. compare — deterministic SI vs draft BL comparison
# ======================================================================

def _equal(field_name: str, a, b) -> bool:
    ka, kb = a.key, b.key

    if field_name in (
        "shipper",
        "consignee",
        "notify_party",
    ):
        return ka["text"] == kb["text"]

    if field_name in (
        "port_of_loading",
        "port_of_discharge",
    ):
        if ka["name"] != kb["name"]:
            return False

        return (
            ka["code"] is None
            or kb["code"] is None
            or ka["code"] == kb["code"]
        )

    if field_name == "container_count":
        if ka["count"] != kb["count"]:
            return False

        return (
            ka["type"] is None
            or kb["type"] is None
            or ka["type"] == kb["type"]
        )

    if field_name == "gross_weight_kg":
        if ka["kg"] is None or kb["kg"] is None:
            return False

        return abs(ka["kg"] - kb["kg"]) < 0.5

    raise ValueError(
        f"unknown field {field_name}"
    )


def compare_fields(
    si_fields: dict,
    bl_fields: dict,
) -> dict:
    items = []

    for f in FIELDS:
        si = si_fields[f]
        bl = bl_fields[f]

        n_si = normalize_value(
            f,
            si["lines"],
        )

        n_bl = normalize_value(
            f,
            bl["lines"],
        )

        items.append({
            "field": f,
            "si_raw": si["full"],
            "bl_raw": bl["full"],
            "si_normalized": n_si.display,
            "bl_normalized": n_bl.display,
            "is_match": _equal(
                f,
                n_si,
                n_bl,
            ),
        })

    defect_fields = [
        i["field"]
        for i in items
        if not i["is_match"]
    ]

    return {
        "result": (
            "MISMATCH"
            if defect_fields
            else "MATCH"
        ),
        "mismatch_count": len(defect_fields),
        "defect_fields": defect_fields,
        "items": items,
    }


# ======================================================================
# 6. classify — email -> category (rule-based)
# ======================================================================

CATEGORIES = (
    "BL_COMPARISON",
    "SI_REQUEST",
    "INVOICE_QUERY",
    "GENERAL",
    "SPAM",
)

_BANNER = re.compile(
    r"^\s*WARNING:\s*This email originated outside[^\n]*\n+",
    re.I,
)

_QUOTE_SPLIT = re.compile(
    r"\n\s*_{5,}\s*\n|\n\s*From:\s.+\n\s*Sent:\s",
    re.I,
)

_GREETING = re.compile(
    r"^\s*(dear|hi|hello)\b[^\n]{0,40}$",
    re.I,
)

_SIGNOFF = re.compile(
    r"^\s*(best regards|regards|thank you|thanks|best|kind regards)"
    r"\b[,.]?\s*$",
    re.I,
)


def clean_body(body: str) -> str:
    text = _BANNER.sub(
        "",
        body or "",
    )

    text = _QUOTE_SPLIT.split(
        text,
        maxsplit=1,
    )[0]

    paras = [
        p.strip()
        for p in re.split(
            r"\n\s*\n",
            text,
        )
        if p.strip()
    ]

    kept = []

    for p in paras:
        first_line = p.splitlines()[0]

        if _GREETING.match(p):
            continue

        if _SIGNOFF.match(first_line):
            break

        kept.append(p)

    return "\n\n".join(kept)


def _strip_reply_prefix(subject: str) -> str:
    return re.sub(
        r"^\s*((re|fw|fwd)\s*[:_]\s*)+",
        "",
        subject or "",
        flags=re.I,
    ).strip()


_R = re.IGNORECASE

RULES = [
    (
        "BL_COMPARISON",
        5,
        "body",
        re.compile(
            r"compare the SI and (the )?draft BL",
            _R,
        ),
        "asks to compare SI and draft BL",
    ),
    (
        "BL_COMPARISON",
        5,
        "body",
        re.compile(
            r"check the draft BL against the SI",
            _R,
        ),
        "asks to check draft BL against SI",
    ),
    (
        "BL_COMPARISON",
        5,
        "body",
        re.compile(
            r"(attached|find attached)[^.\n]*\bSI\b[^.\n]*(draft )?(BL|bill of lading)",
            _R,
        ),
        "SI + draft BL attached",
    ),
    (
        "BL_COMPARISON",
        5,
        "body",
        re.compile(
            r"draft (BL|bill of lading)[^.\n]*\bfor (checking|confirmation)\b",
            _R,
        ),
        "draft BL sent/requested for checking",
    ),
    (
        "BL_COMPARISON",
        3,
        "body",
        re.compile(
            r"confirm the BL (matches|is in order)|"
            r"verify the BL matches the SI",
            _R,
        ),
        "asks to confirm BL vs SI",
    ),
    (
        "BL_COMPARISON",
        2,
        "subject",
        re.compile(
            r"TO CONFIRM DOCS|REQUEST BL DRAFT|\bDRAFT BL\b",
            _R,
        ),
        "subject: BL check",
    ),

    (
        "SI_REQUEST",
        6,
        "body",
        re.compile(
            r"^\s*Please find Shipping instruction for",
            _R | re.M,
        ),
        "body: SI request",
    ),
    (
        "SI_REQUEST",
        2,
        "body",
        re.compile(
            r"^\s*(POL|POD)\s*:.*$",
            _R | re.M,
        ),
        "inline POL/POD fields",
    ),
    (
        "SI_REQUEST",
        3,
        "subject",
        re.compile(
            r"^\s*(CUST SI|REQUEST SI|SI\s*-)",
            _R,
        ),
        "subject: SI request",
    ),

    (
        "INVOICE_QUERY",
        5,
        "body",
        re.compile(
            r"\binvoice\s+#?\d{6,}",
            _R,
        ),
        "body: invoice number",
    ),
    (
        "INVOICE_QUERY",
        4,
        "body",
        re.compile(
            r"\bGR\b is still missing|post the GR|reverse the PGI",
            _R,
        ),
        "body: GR/PGI billing issue",
    ),
    (
        "INVOICE_QUERY",
        4,
        "body",
        re.compile(
            r"\bTHC\b|local charge|D&D|detention charges",
            _R,
        ),
        "body: THC/local/D&D charges",
    ),
    (
        "INVOICE_QUERY",
        3,
        "subject",
        re.compile(
            r"RAK BILLING|LOCAL CHARGES|TOTAL FREIGHT|"
            r"CANCEL INVOICE|MILL D ?&? ?D|MISSING GR",
            _R,
        ),
        "subject: billing/charges",
    ),

    (
        "SPAM",
        6,
        "body",
        re.compile(
            r"https?://\S+",
            _R,
        ),
        "body: link",
    ),
    (
        "SPAM",
        6,
        "body",
        re.compile(
            r"you have won|selected in our monthly draw|claim your|"
            r"bank officer|business proposal|unpaid customs fee|"
            r"mailbox has exceeded|verify your account|limited time offer|"
            r"guaranteed \d+% returns|reply with your bank details",
            _R,
        ),
        "body: scam / phishing wording",
    ),
    (
        "SPAM",
        4,
        "subject",
        re.compile(
            r"bitcoin|guaranteed \d+% returns|ONE weird trick|"
            r"90% OFF|hot singles|urgent: your email|"
            r"congratulations|you have \d+|dear valued customer",
            _R,
        ),
        "subject: spam wording",
    ),
    (
        "SPAM",
        3,
        "sender",
        re.compile(
            r"@(crypto-invest|secure-mailbox|webmail-verify|prize-claims)"
            r"|\.(info|xyz|top)$",
            _R,
        ),
        "sender: suspicious domain",
    ),

    (
        "GENERAL",
        5,
        "body",
        re.compile(
            r"daily berthing report|update summary for|list of outstanding BL",
            _R,
        ),
        "body: operational report",
    ),
    (
        "GENERAL",
        5,
        "body",
        re.compile(
            r"automated notification|-- RPA Bot|no action required",
            _R,
        ),
        "body: automated notification",
    ),
    (
        "GENERAL",
        5,
        "body",
        re.compile(
            r"Reminder: Please submit SI & AED",
            _R,
        ),
        "body: internal reminder",
    ),
    (
        "GENERAL",
        5,
        "body",
        re.compile(
            r"happy and prosperous New Year|Office resumes normal operations",
            _R,
        ),
        "body: holiday notice",
    ),
    (
        "GENERAL",
        2,
        "subject",
        re.compile(
            r"UPDATE SUMMARY|BERTHING REPORT|PENDING BL RELEASE|"
            r"_RPA_|Time Off Request|Delivery planning|"
            r"Welcoming the New Year|_Reminder_",
            _R,
        ),
        "subject: operational",
    ),
]


def _score(email: dict):
    subject = _strip_reply_prefix(
        email.get("subject", "")
    )

    body = clean_body(
        email.get("body", "")
    )

    sender = email.get("from", "") or ""

    n_att = len(
        email.get("attachments") or []
    )

    where = {
        "body": body,
        "subject": subject,
        "sender": sender,
    }

    scores = {
        c: 0
        for c in CATEGORIES
    }

    signals = []

    for cat, weight, source, rx, label in RULES:
        if rx.search(where[source]):
            scores[cat] += weight
            signals.append(
                (cat, weight, label)
            )

    if (
        n_att
        and re.search(
            r"\bSI\b|shipping instruction",
            body,
            _R,
        )
        and re.search(
            r"\bBL\b|bill of lading",
            body,
            _R,
        )
    ):
        scores["BL_COMPARISON"] += 3
        signals.append(
            (
                "BL_COMPARISON",
                3,
                f"{n_att} attachment(s) with SI/BL wording",
            )
        )

    if (
        scores["SPAM"]
        and not re.search(
            r"you have won|selected in our monthly draw|claim your|"
            r"bank officer|business proposal|unpaid customs|"
            r"mailbox has exceeded|verify your account|"
            r"limited time|bitcoin|guaranteed|bank details",
            body,
            _R,
        )
        and not re.search(
            r"bitcoin|weird trick|90% off|hot singles|"
            r"urgent: your email|congratulations|valued customer",
            subject,
            _R,
        )
    ):
        scores["SPAM"] = 0
        signals = [
            s
            for s in signals
            if s[0] != "SPAM"
        ]

    return scores, signals, subject, body


def classify(email: dict) -> dict:
    scores, signals, _, _ = _score(email)

    ranked = sorted(
        scores.items(),
        key=lambda kv: kv[1],
        reverse=True,
    )

    (best, best_score), (_, second_score) = ranked[:2]

    if best_score == 0:
        return {
            "category": "GENERAL",
            "confidence": 0.0,
            "needs_llm": True,
            "scores": scores,
            "signals": signals,
        }

    margin = best_score - second_score

    confidence = round(
        min(
            1.0,
            0.5 + margin / 12,
        ),
        2,
    )

    return {
        "category": best,
        "confidence": confidence,
        "needs_llm": margin < 3,
        "scores": scores,
        "signals": signals,
    }# ======================================================================
# 7. nlp — optional Laya model classification, hybrid with classify()
# ======================================================================

os.environ.setdefault("USE_TF", "0")

DEFAULT_MODEL = "convaiinnovations/laya"

OPTIONS = {
    "check_bl_against_si": (
        "BL_COMPARISON",
        "asks to check or compare a draft bill of lading against a shipping instruction",
    ),
    "new_shipping_instruction": (
        "SI_REQUEST",
        "sends details of a new shipment or shipping instruction to process",
    ),
    "invoice_query": (
        "INVOICE_QUERY",
        "question about an invoice, charges, billing or a missing goods receipt",
    ),
    "internal_update": (
        "GENERAL",
        "internal operational notice, report, reminder or automated notification",
    ),
    "spam": (
        "SPAM",
        "scam, phishing, prize or unsolicited advertising",
    ),
}

QUESTIONS = {
    "category": {
        "type": "choice",
        "instructions": "What kind of email is this?",
        "criteria": {
            name: desc
            for name, (_, desc) in OPTIONS.items()
        },
    }
}

MODEL_IF_RULES_BELOW = 0.95
MODEL_MIN_CONFIDENCE = 0.85
MAX_BODY_CHARS = 1200

_agent = None


def _model_id() -> str:
    model = os.environ.get("NLP_MODEL", "")

    if not model:
        env = Path(__file__).resolve().parent / ".env"

        if env.is_file():
            for line in env.read_text(
                encoding="utf-8"
            ).splitlines():
                if line.strip().startswith("NLP_MODEL="):
                    model = (
                        line.split("=", 1)[1]
                        .strip()
                        .strip("'\"")
                    )

    return model or DEFAULT_MODEL


def set_agent(agent):
    global _agent
    _agent = agent


def _load():
    global _agent

    if _agent is None:
        try:
            import laya
        except ImportError as exc:
            raise RuntimeError(
                "Install Laya first: "
                "python -m pip install laya"
            ) from exc

        _agent = laya.load(_model_id())

    return _agent


def _state(email: dict) -> dict:
    return {
        "from": email.get("from", ""),
        "subject": email.get("subject", ""),
        "body": clean_body(
            email.get("body", "")
        )[:MAX_BODY_CHARS],
        "attachments": len(
            email.get("attachments") or []
        ),
    }


def model_classify(email: dict) -> dict:
    answer = (
        _load()
        .predict(_state(email), QUESTIONS)
        ["answers"]["category"]
    )

    scores = {
        OPTIONS[name][0]: float(probability)
        for name, probability in (
            answer.get("probabilities") or {}
        ).items()
    }

    choice = answer.get("choice")

    if choice not in OPTIONS:
        raise RuntimeError(
            f"Laya returned unknown category: {choice!r}"
        )

    category = OPTIONS[choice][0]

    score = float(
        answer.get(
            "confidence",
            scores.get(category, 0.0),
        )
    )

    return {
        "category": category,
        "score": round(score, 3),
        "scores": scores,
    }


def classify_hybrid(email: dict) -> dict:
    result = classify(email)
    result["source"] = "rules"

    # Strong deterministic classification:
    # do not call Laya.
    if (
        result["confidence"] >= MODEL_IF_RULES_BELOW
        and not result["needs_llm"]
    ):
        return result

    # Laya is optional. If it is unavailable or fails,
    # retain the deterministic classification.
    try:
        model_result = model_classify(email)
    except Exception as exc:
        result["model_error"] = str(exc)
        result["source"] = "rules"
        return result

    result["model_scores"] = model_result["scores"]

    if model_result["score"] >= MODEL_MIN_CONFIDENCE:
        result.update(
            category=model_result["category"],
            confidence=model_result["score"],
            source="model",
            needs_llm=False,
        )

    return result


# ======================================================================
# 8. cases — group BL_COMPARISON emails into cases, plan DB rows
# ======================================================================

NAMESPACE = uuid.UUID(
    "6f1c2b7e-0d3a-4c58-9a1e-5b2f8d4e7a10"
)


def uid(kind: str, *parts: str) -> str:
    return str(
        uuid.uuid5(
            NAMESPACE,
            kind + ":" + ":".join(parts),
        )
    )


_OC = re.compile(
    r"\b5[A-Z]{3}-\d{5}\b"
)

_BL_NO = re.compile(
    r"(?:bill of lading no\.?|b/l no\.?|b/l number|bl no\.?)"
    r"[^:\n]{0,20}:\s*([A-Z0-9]{6,})",
    re.I,
)

_BOOKING = re.compile(
    r"booking\s*(?:ref(?:erence)?|no\.?)"
    r"[^:\n]{0,10}[: ]\s*([A-Z0-9]{6,})",
    re.I,
)


def find_refs(text: str) -> dict:
    text = text or ""

    return {
        "oc": sorted(
            set(_OC.findall(text))
        ),
        "bl_no": sorted(
            {
                match.upper()
                for match in _BL_NO.findall(text)
            }
        ),
        "booking": sorted(
            {
                match.upper()
                for match in _BOOKING.findall(text)
            }
        ),
    }


def email_refs(
    email: dict,
    result: dict,
) -> set:
    text = (
        (email.get("subject") or "")
        + "\n"
        + (email.get("body") or "")
    )

    found = set(
        _OC.findall(text)
    )

    for document in result.get(
        "documents",
        [],
    ):
        for values in (
            document.get("refs") or {}
        ).values():
            found.update(values)

    return found


def case_ref_for(email_id: str) -> str:
    suffix = str(email_id).split("_")[-1]

    try:
        return "SH-" + str(
            1000 + int(suffix)
        )
    except ValueError:
        return "SH-" + str(
            abs(
                uuid.uuid5(
                    NAMESPACE,
                    email_id,
                ).int
            ) % 900000 + 100000
        )


def case_state(result: dict):
    status = result["status"]
    reason = result["review_reason"]

    if status == "OK":
        return "RESOLVED", "MATCHED"

    if (
        status == "MISMATCH"
        or reason == "missing_value"
    ):
        return "AWAITING_REVIEW", None

    return "AWAITING_DOCUMENT", None


_DECISIONS = {
    "discrepancy": [
        "ACCEPT_OVERRIDE",
        "AUTHORIZE_CORRECTION",
    ],
    "missing_attachment": [
        "MISSING_DOCUMENT",
    ],
    "wrong_doc_type": [
        "MISSING_DOCUMENT",
    ],
    "unreadable": [
        "MISSING_DOCUMENT",
    ],
    "missing_value": [
        "CORRECT_EXTRACTION",
        "MISSING_DOCUMENT",
    ],
}

_REASON_TEXT = {
    "missing_attachment": (
        "The SI or the draft BL is missing, "
        "so the two cannot be compared."
    ),
    "wrong_doc_type": (
        "An attachment is not a Shipping Instruction "
        "or a Bill of Lading."
    ),
    "unreadable": (
        "An attachment could not be read "
        "(corrupt file or image-only scan)."
    ),
    "missing_value": (
        "A required field is blank or missing "
        "in the SI or BL."
    ),
}


class _Clock:
    def __init__(self, start):
        self.t = start
        self.start = start

    def tick(self, seconds=8):
        self.t = self.t + timedelta(
            seconds=seconds
        )
        return self.t.isoformat()


def plan_case(
    email: dict,
    result: dict,
    case_id: str,
    email_id_uuid: str,
    start: datetime,
) -> dict:
    eid = email["email_id"]
    clock = _Clock(start)

    status = result["status"]
    reason = result["review_reason"]
    docs = result["documents"]

    case_status, resolution = case_state(
        result
    )

    refs = sorted(
        email_refs(
            email,
            result,
        )
    )

    rows = {
        table: []
        for table in (
            "cases",
            "emails",
            "documents",
            "activities",
            "extracted_fields",
            "validation_results",
            "comparison_runs",
            "comparison_items",
            "review_tasks",
            "agent_actions",
            "audit_events",
        )
    }

    def doc_uuid(document):
        return uid(
            "doc",
            "attachments/" + document["file"],
        )

    def activity(
        activity_type,
        state,
        doc=None,
        attempts=1,
        blocked=None,
        error=None,
    ):
        aid = uid(
            "activity",
            case_id,
            activity_type,
            doc_uuid(doc) if doc else "",
        )

        started = clock.tick()

        rows["activities"].append({
            "id": aid,
            "case_id": case_id,
            "document_id": (
                doc_uuid(doc)
                if doc
                else None
            ),
            "activity_type": activity_type,
            "state": state,
            "attempt_count": (
                0
                if (
                    state == "BLOCKED"
                    or activity_type == "HUMAN_REVIEW"
                )
                else attempts
            ),
            "blocked_reason": blocked,
            "last_error": error,
            "started_at": (
                None
                if state in ("BLOCKED", "CREATED")
                else started
            ),
            "completed_at": (
                clock.t.isoformat()
                if state == "COMPLETED"
                else None
            ),
        })

        return aid

    def agent(tool, tool_in, tool_out):
        rows["agent_actions"].append({
            "case_id": case_id,
            "tool_name": tool,
            "tool_input": tool_in,
            "tool_output": tool_out,
            "allowed": True,
            "rejection_reason": None,
            "created_at": clock.tick(2),
        })

    def audit(
        actor,
        actor_id,
        event,
        entity_type,
        entity,
        frm,
        to,
        details,
    ):
        rows["audit_events"].append({
            "case_id": case_id,
            "actor": actor,
            "actor_id": actor_id,
            "event_type": event,
            "entity_type": entity_type,
            "entity_id": entity,
            "from_state": frm,
            "to_state": to,
            "details": details,
            "created_at": clock.tick(1),
        })

    rows["cases"].append({
        "id": case_id,
        "case_ref": case_ref_for(eid),
        "shipment_ref": (
            refs[0]
            if refs
            else None
        ),
        "title": (
            email.get("subject") or ""
        )[:200],
        "status": case_status,
        "resolution": resolution,
        "resolution_notes": (
            "Auto-resolved: all 7 fields match."
            if resolution
            else None
        ),
        "resolved_at": (
            clock.tick()
            if resolution
            else None
        ),
        "closed_at": None,
    })

    rows["emails"].append({
        "id": email_id_uuid,
        "case_id": case_id,
        "case_relationship": "NEW_CASE",
        "category": result["category"],
        "classification_confidence": result["confidence"],
    })

    audit(
        "SYSTEM",
        "mailbox",
        "EMAIL_RECEIVED",
        "email",
        email_id_uuid,
        None,
        None,
        {
            "email_id": eid,
            "subject": email.get("subject"),
            "attachments": len(docs),
        },
    )

    agent(
        "classify_email",
        {"email_id": eid},
        {
            "category": result["category"],
            "confidence": result["confidence"],
        },
    )

    agent(
        "create_case",
        {"email_id": eid},
        {
            "case_ref": case_ref_for(eid),
            "shipment_refs": refs,
        },
    )

    audit(
        "AGENT",
        "pipeline",
        "CASE_CREATED",
        "case",
        case_id,
        None,
        "OPEN",
        {
            "case_ref": case_ref_for(eid),
            "shipment_refs": refs,
        },
    )

    activity(
        "EMAIL_CLASSIFICATION",
        "COMPLETED",
    )

    for document in docs:
        rows["documents"].append({
            "id": doc_uuid(document),
            "case_id": case_id,
            "doc_type": document["doc_type"],
            "type_confidence": document["type_confidence"],
        })

        agent(
            "identify_document_type",
            {"file": document["file"]},
            {
                "doc_type": document["doc_type"],
                "confidence": document["type_confidence"],
                "error": document["error"],
            },
        )

    if not docs:
        activity(
            "DOCUMENT_IDENTIFICATION",
            "BLOCKED",
            blocked="The email has no attachments",
        )
    elif reason == "missing_attachment":
        activity(
            "DOCUMENT_IDENTIFICATION",
            "BLOCKED",
            blocked="The SI or the draft BL is missing",
        )
    elif reason == "wrong_doc_type":
        activity(
            "DOCUMENT_IDENTIFICATION",
            "REQUIRES_REVIEW",
            error="An attachment is not an SI or a BL",
        )
    else:
        activity(
            "DOCUMENT_IDENTIFICATION",
            "COMPLETED",
        )

    if docs:
        audit(
            "AGENT",
            "pipeline",
            "DOCUMENTS_IDENTIFIED",
            "case",
            case_id,
            None,
            None,
            {
                d["file"]: d["doc_type"]
                for d in docs
            },
        )

    extraction = {}

    for document in docs:
        slot = (
            document["doc_type"]
            if document["doc_type"] in ("SI", "BL")
            else (
                document["slot"]
                if document.get("error")
                else None
            )
        )

        if slot not in ("SI", "BL"):
            continue

        ok = (
            document["state"]
            == "COMPLETED"
        )

        error_text = None

        if not ok:
            error_text = (
                document.get("error")
                or ", ".join(
                    f"{issue['field']} {issue['problem']}"
                    for issue in document.get(
                        "issues",
                        [],
                    )
                )
            )

        aid = activity(
            f"{slot}_EXTRACTION",
            "COMPLETED"
            if ok
            else "REQUIRES_REVIEW",
            doc=document,
            attempts=max(
                document["attempts"],
                1,
            ),
            error=error_text,
        )

        extraction[document["file"]] = (
            aid,
            document,
        )

        agent(
            "extract_fields",
            {"file": document["file"]},
            {
                "state": document["state"],
                "attempts": document["attempts"],
                "issues": document["issues"],
                "error": document["error"],
            },
        )

        audit(
            "SYSTEM",
            "workflow",
            "ACTIVITY_STATE_CHANGED",
            "activity",
            aid,
            "CREATED",
            "COMPLETED"
            if ok
            else "REQUIRES_REVIEW",
            {
                "activity": f"{slot}_EXTRACTION",
                "file": document["file"],
            },
        )

    readable = [
        (aid, document)
        for aid, document in extraction.values()
        if document["fields"]
    ]

    if readable:
        all_valid = all(
            document["validation_status"]
            == "VALID"
            for _, document in readable
        )

        validation_activity_id = activity(
            "EXTRACTION_VALIDATION",
            "COMPLETED"
            if all_valid
            else "REQUIRES_REVIEW",
        )

        for aid, document in readable:
            attempt = max(
                document["attempts"],
                1,
            )

            for field_name in FIELDS:
                item = document["fields"][field_name]

                usable = (
                    item["present"]
                    and not item["blank"]
                )

                norm = (
                    normalize_value(
                        field_name,
                        item["lines"],
                    )
                    if usable
                    else None
                )

                rows["extracted_fields"].append({
                    "id": uid(
                        "field",
                        doc_uuid(document),
                        field_name,
                        str(attempt),
                    ),
                    "document_id": doc_uuid(document),
                    "activity_id": aid,
                    "field_name": field_name,
                    "raw_value": (
                        item["full"]
                        or None
                    ),
                    "normalized_value": (
                        norm.display
                        if norm
                        else None
                    ),
                    "numeric_value": (
                        norm.numeric
                        if norm
                        else None
                    ),
                    "confidence": item["confidence"],
                    "source_page": None,
                    "extraction_attempt": attempt,
                    "is_human_corrected": False,
                    "is_current": True,
                })

            rows["validation_results"].append({
                "id": uid(
                    "validation",
                    doc_uuid(document),
                    str(attempt),
                ),
                "activity_id": validation_activity_id,
                "document_id": doc_uuid(document),
                "result": (
                    document["validation_status"]
                    or "INVALID"
                ),
                "issues": document["issues"],
                "attempt": attempt,
            })

    comparison = result["comparison"]

    if comparison:
        si = next(
            d
            for d in docs
            if d["doc_type"] == "SI"
        )

        bl = next(
            d
            for d in docs
            if d["doc_type"] == "BL"
        )

        comparison_activity_id = activity(
            "COMPARISON",
            "COMPLETED",
        )

        run_id = uid(
            "comparison",
            case_id,
        )

        rows["comparison_runs"].append({
            "id": run_id,
            "case_id": case_id,
            "activity_id": comparison_activity_id,
            "si_document_id": doc_uuid(si),
            "bl_document_id": doc_uuid(bl),
            "result": comparison["result"],
            "mismatch_count": comparison[
                "mismatch_count"
            ],
        })

        for item in comparison["items"]:
            rows["comparison_items"].append({
                "id": uid(
                    "cmpitem",
                    run_id,
                    item["field"],
                ),
                "run_id": run_id,
                "field_name": item["field"],
                "si_raw": item["si_raw"],
                "bl_raw": item["bl_raw"],
                "si_normalized": item[
                    "si_normalized"
                ],
                "bl_normalized": item[
                    "bl_normalized"
                ],
                "is_match": item["is_match"],
            })

        agent(
            "compare_fields",
            {},
            {
                "result": comparison["result"],
                "defect_fields": comparison[
                    "defect_fields"
                ],
            },
        )

        audit(
            "SYSTEM",
            "comparison",
            "COMPARISON_COMPLETED",
            "comparison_run",
            run_id,
            None,
            None,
            {
                "result": comparison["result"],
                "defect_fields": comparison[
                    "defect_fields"
                ],
            },
        )

        if comparison["defect_fields"]:
            activity(
                "DISCREPANCY_REPORTING",
                "COMPLETED",
            )

            agent(
                "generate_discrepancy_report",
                {"run_id": run_id},
                {
                    "mismatch_count": comparison[
                        "mismatch_count"
                    ]
                },
            )

    else:
        activity(
            "COMPARISON",
            "BLOCKED",
            blocked=f"Cannot compare: {reason}",
        )

    if status != "OK":
        human_review_id = activity(
            "HUMAN_REVIEW",
            "REQUIRES_REVIEW",
        )

        key = (
            "discrepancy"
            if status == "MISMATCH"
            else reason
        )

        if status == "MISMATCH":
            description = (
                "SI and draft BL differ on: "
                + ", ".join(
                    result["defect_fields"]
                )
                + "."
            )
        else:
            description = _REASON_TEXT.get(
                reason,
                "Manual review is required.",
            )

        task_id = uid(
            "review",
            case_id,
        )

        rows["review_tasks"].append({
            "id": task_id,
            "case_id": case_id,
            "activity_id": human_review_id,
            "reason": key,
            "description": description,
            "context": {
                "status": status,
                "review_reason": reason,
                "defect_fields": result[
                    "defect_fields"
                ],
                "suggested_decisions": (
                    _DECISIONS.get(key, [])
                ),
                "documents": [
                    {
                        "file": d["file"],
                        "slot": d["slot"],
                        "doc_type": d["doc_type"],
                        "state": d["state"],
                    }
                    for d in docs
                ],
            },
            "status": "OPEN",
            "assigned_to": None,
            "assigned_at": None,
            "resolved_at": None,
        })

        agent(
            "request_human_review",
            {"reason": key},
            {
                "review_task_id": task_id
            },
        )

        audit(
            "AGENT",
            "pipeline",
            "HUMAN_REVIEW_REQUESTED",
            "review_task",
            task_id,
            None,
            "OPEN",
            {
                "reason": key,
                "suggested_decisions": (
                    _DECISIONS.get(key, [])
                ),
            },
        )

    else:
        agent(
            "generate_final_result",
            {},
            {"resolution": "MATCHED"},
        )

    audit(
        "SYSTEM",
        "workflow",
        "CASE_STATE_CHANGED",
        "case",
        case_id,
        "OPEN",
        case_status,
        {
            "status": status,
            "review_reason": reason,
            "defect_fields": result[
                "defect_fields"
            ],
        },
    )

    return rows


def sort_into_cases(
    emails: list,
    results: dict,
    start: datetime = None,
):
    start = (
        start
        or datetime.now(timezone.utc)
    )

    known = {}
    plans = {}
    links = []

    for n, email in enumerate(emails):
        result = results[
            email["email_id"]
        ]

        if result["category"] != "BL_COMPARISON":
            continue

        refs = email_refs(
            email,
            result,
        )

        existing = next(
            (
                known[r]
                for r in sorted(refs)
                if r in known
            ),
            None,
        )

        if existing:
            links.append(
                (
                    email["email_id"],
                    existing,
                    "EXISTING_CASE",
                )
            )
            continue

        case_id = uid(
            "case",
            email["email_id"],
        )

        for ref in refs:
            known[ref] = case_id

        email_uuid = uid(
            "email",
            email["email_id"],
        )

        plans[case_id] = plan_case(
            email,
            result,
            case_id,
            email_uuid,
            start + timedelta(seconds=n),
        )

        links.append(
            (
                email["email_id"],
                case_id,
                "NEW_CASE",
            )
        )

    return plans, links


# ======================================================================
# 9. pipeline — per-email orchestration (entry point)
# ======================================================================

REVIEW_REASONS = (
    "wrong_doc_type",
    "missing_attachment",
    "unreadable",
    "missing_value",
)


def _slot(path: str) -> str:
    name = Path(path).name.upper()

    if "_SI." in name:
        return "SI"

    if "_BL." in name:
        return "BL"

    return "?"


def _read_attachment(
    path: str,
    read_bytes: Callable[[str], bytes],
) -> dict:
    try:
        data = read_bytes(path)

        doc = read_document_bytes(
            data,
            Path(path).name,
        )

    except FileNotFoundError:
        doc = Doc(
            kind=Path(path).suffix.lstrip("."),
            name=Path(path).name,
            error="file_not_found",
        )

    result = process_document(doc)

    result["slot"] = _slot(path)
    result["refs"] = find_refs(
        doc.text
    )

    return result


def escalation_reason(
    docs: list,
) -> Optional[str]:
    readable = [
        d
        for d in docs
        if not d.get("error")
    ]

    unreadable = [
        d
        for d in docs
        if d.get("error")
    ]

    si = [
        d
        for d in readable
        if d["doc_type"] == "SI"
    ]

    bl = [
        d
        for d in readable
        if d["doc_type"] == "BL"
    ]

    if (
        any(
            d["doc_type"]
            not in ("SI", "BL")
            for d in readable
        )
        or len(si) > 1
        or len(bl) > 1
    ):
        return "wrong_doc_type"

    if (
        len(docs) < 2
        or (
            not unreadable
            and (
                not si
                or not bl
            )
        )
    ):
        return "missing_attachment"

    if unreadable:
        return "unreadable"

    if any(
        d["state"] != "COMPLETED"
        for d in si + bl
    ):
        return "missing_value"

    return None


def process_email(
    email: dict,
    read_bytes: Callable[[str], bytes],
) -> dict:
    cls = classify_hybrid(email)

    result = {
        "email_id": email["email_id"],
        "category": cls["category"],
        "confidence": cls["confidence"],
        "classification_source": cls.get(
            "source",
            "rules",
        ),
        "status": "OK",
        "review_reason": None,
        "has_defect": False,
        "defect_fields": [],
        "documents": [],
        "comparison": None,
        "trace": [
            (
                f"classified as "
                f"{cls['category']} "
                f"(confidence "
                f"{cls['confidence']})"
            )
        ],
    }

    if cls.get("model_error"):
        result["trace"].append(
            "Laya unavailable/failed; "
            "continued with rule classification"
        )

    if cls["category"] != "BL_COMPARISON":
        result["trace"].append(
            "no SI/BL comparison needed"
        )
        return result

    attachments = (
        email.get("attachments")
        or []
    )

    docs = [
        _read_attachment(
            path,
            read_bytes,
        )
        for path in attachments
    ]

    result["documents"] = [
        {
            "file": d["file"],
            "slot": d["slot"],
            "doc_type": d["doc_type"],
            "state": d["state"],
            "type_confidence": d["confidence"],
            "error": d.get("error"),
            "review_reason": d.get(
                "review_reason"
            ),
            "issues": (
                d["validation"] or {}
            ).get(
                "issues",
                [],
            ),
            "validation_status": (
                d["validation"] or {}
            ).get("status"),
            "attempts": len([
                a
                for a in d["attempts"]
                if a["status"] != "SKIPPED"
            ]),
            "fields": d["fields"],
            "refs": d.get(
                "refs",
                {},
            ),
        }
        for d in docs
    ]

    for document in docs:
        note = (
            f"error: {document['error']}"
            if document.get("error")
            else f"state {document['state']}"
        )

        result["trace"].append(
            f"{document['file']}: "
            f"identified as "
            f"{document['doc_type']}, "
            f"{note}"
        )

    reason = escalation_reason(
        docs
    )

    if reason:
        result.update(
            status="NEEDS_REVIEW",
            review_reason=reason,
        )

        result["trace"].append(
            "cannot decide automatically -> "
            f"NEEDS_REVIEW ({reason})"
        )

        return result

    si = next(
        d
        for d in docs
        if d["doc_type"] == "SI"
    )

    bl = next(
        d
        for d in docs
        if d["doc_type"] == "BL"
    )

    comparison = compare_fields(
        si["fields"],
        bl["fields"],
    )

    result["comparison"] = comparison
    result["defect_fields"] = (
        comparison["defect_fields"]
    )
    result["has_defect"] = bool(
        comparison["defect_fields"]
    )

    result["status"] = (
        "MISMATCH"
        if comparison["defect_fields"]
        else "OK"
    )

    result["trace"].append(
        f"compared 7 fields -> "
        f"{result['status']}"
        + (
            " ("
            + ", ".join(
                comparison["defect_fields"]
            )
            + ")"
            if comparison["defect_fields"]
            else ""
        )
    )

    return result


def to_submission_entry(
    result: dict,
) -> dict:
    return {
        "category": result["category"],
        "status": result["status"],
        "review_reason": result[
            "review_reason"
        ],
        "defect_fields": result[
            "defect_fields"
        ],
        "has_defect": result[
            "has_defect"
        ],
    }


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

from flask import Flask, jsonify, request

http_app = Flask(__name__)


@http_app.get("/health")
def http_health():
    return jsonify({
        "status": "ok",
        "service": "python-sidecar",
    })


@http_app.post("/process-email")
def http_process_email():
    try:
        payload = request.get_json(silent=True)

        if not isinstance(payload, dict):
            return jsonify({
                "error": "Request body must be a JSON object."
            }), 400

        if not payload.get("email_id"):
            return jsonify({
                "error": "email_id is required."
            }), 400

        attachments = payload.get("attachments", [])

        if not isinstance(attachments, list):
            return jsonify({
                "error": "attachments must be an array."
            }), 400

        import base64

        attachment_bytes = {}

        for attachment in attachments:
            if not isinstance(attachment, dict):
                return jsonify({
                    "error": "Each attachment must be an object."
                }), 400

            storage_path = attachment.get("storage_path")
            content_base64 = attachment.get("content_base64")

            if not storage_path:
                return jsonify({
                    "error": "attachment storage_path is required."
                }), 400

            if not content_base64:
                return jsonify({
                    "error": f"Missing content_base64 for {storage_path}"
                }), 400

            try:
                attachment_bytes[storage_path] = base64.b64decode(
                    content_base64
                )
            except Exception:
                return jsonify({
                    "error": f"Invalid base64 attachment: {storage_path}"
                }), 400

        def read_bytes(path: str) -> bytes:
            if path in attachment_bytes:
                return attachment_bytes[path]

            file_name = Path(path).name

            for supplied_path, data in attachment_bytes.items():
                if Path(supplied_path).name == file_name:
                    return data

            raise FileNotFoundError(
                f"Attachment not supplied: {path}"
            )

        normalized_email = dict(payload)

        normalized_email["attachments"] = [
            attachment["storage_path"]
            for attachment in attachments
        ]

        result = process_email(
            normalized_email,
            read_bytes,
        )

        return jsonify(result), 200

    except FileNotFoundError as exc:
        return jsonify({
            "error": "attachment_not_found",
            "message": str(exc),
        }), 400

    except Exception as exc:
        import traceback

        traceback.print_exc()

        return jsonify({
            "error": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }), 500

if __name__ == "__main__":
    host = os.environ.get("SIDECAR_HOST", "127.0.0.1")
    port = int(os.environ.get("SIDECAR_PORT", "8001"))

    http_app.run(
        host=host,
        port=port,
        debug=False,
    )