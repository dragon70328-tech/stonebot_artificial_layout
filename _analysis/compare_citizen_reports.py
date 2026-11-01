"""One-off analysis: parse Citizen nesting report (docx) and manual slab usage (xlsx).

Stdlib only (zipfile + ElementTree) so no third-party install is needed.
"""

from __future__ import annotations

import re
import sys
import zipfile
import xml.etree.ElementTree as ET

NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NS_A = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def docx_text(path: str) -> list[str]:
    lines: list[str] = []
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    body = root.find(f"{NS_W}body")
    for elem in body.iter():
        if elem.tag == f"{NS_W}p":
            text = "".join(t.text or "" for t in elem.iter(f"{NS_W}t"))
            if text.strip():
                lines.append(text)
        elif elem.tag == f"{NS_W}tbl":
            for row in elem.findall(f"{NS_W}tr"):
                cells = []
                for cell in row.findall(f"{NS_W}tc"):
                    text = "".join(t.text or "" for t in cell.iter(f"{NS_W}t"))
                    cells.append(text.strip())
                lines.append(" | ".join(cells))
    return lines


def xlsx_sheets(path: str) -> dict[str, list[list[str]]]:
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            sroot = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in sroot.findall(f"{NS_A}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{NS_A}t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rel_map = {
            r.get("Id"): r.get("Target")
            for r in rels
            if r.get("Type", "").endswith("/worksheet")
        }
        sheets: dict[str, list[list[str]]] = {}
        for sh in wb.iter(f"{NS_A}sheet"):
            name = sh.get("name")
            rid = sh.get(f"{NS_R}id")
            target = rel_map.get(rid)
            if target is None:
                continue
            spath = "xl/" + target.lstrip("/")
            sroot = ET.fromstring(z.read(spath))
            rows: list[list[str]] = []
            for row in sroot.iter(f"{NS_A}row"):
                cells: dict[int, str] = {}
                for c in row.findall(f"{NS_A}c"):
                    ref = c.get("r", "A1")
                    m = re.match(r"[A-Z]+", ref)
                    col = 0
                    for ch in m.group():
                        col = col * 26 + (ord(ch) - 64)
                    v = c.find(f"{NS_A}v")
                    is_el = c.find(f"{NS_A}is")
                    if c.get("t") == "s" and v is not None:
                        val = shared[int(v.text)]
                    elif c.get("t") == "inlineStr" and is_el is not None:
                        val = "".join(t.text or "" for t in is_el.iter(f"{NS_A}t"))
                    elif v is not None:
                        val = v.text or ""
                    else:
                        val = ""
                    cells[col - 1] = val
                if cells:
                    width = max(cells) + 1
                    rows.append([cells.get(i, "") for i in range(width)])
            sheets[name] = rows
        return sheets


def main() -> None:
    docx_path, xlsx_path = sys.argv[1], sys.argv[2]
    print("=" * 30, "DOCX", "=" * 30)
    for line in docx_text(docx_path):
        print(line)
    print("=" * 30, "XLSX", "=" * 30)
    for name, rows in xlsx_sheets(xlsx_path).items():
        print(f"--- sheet: {name} ({len(rows)} rows) ---")
        for row in rows:
            print(" | ".join(row))


if __name__ == "__main__":
    main()
