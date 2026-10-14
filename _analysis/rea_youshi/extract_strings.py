"""Extract printable / CJK strings from a .NET assembly's CLI resource blob.

Static only: the target assembly is never loaded and no target code runs. The
script walks the PE section table to map the CLI resource RVA the way the loader
does, then scans that blob and the whole file for UTF-8 text runs.

Usage:
    python -X utf8 _analysis/rea_youshi/extract_strings.py <assembly.dll> <out.txt>
"""

import re
import struct
import sys


def sections(data):
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    base = pe + 24 + opt_size
    out = []
    for i in range(nsec):
        off = base + i * 40
        name = data[off:off + 8].rstrip(b"\0").decode("latin-1")
        vsize, vaddr, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
        out.append((name, vaddr, vsize, raw_ptr, raw_size))
    return out


def rva_to_offset(secs, rva):
    for _name, vaddr, vsize, raw_ptr, raw_size in secs:
        if vaddr <= rva < vaddr + max(vsize, raw_size):
            delta = rva - vaddr
            if delta < raw_size:
                return raw_ptr + delta
    return None


def text_runs(blob, min_len=3):
    """Yield UTF-8 runs that contain at least one CJK char or look word-like."""
    cjk = re.compile("[一-鿿　-〿＀-￯]")
    word = re.compile(rb"[\x20-\x7e]{4,}")
    for m in word.finditer(blob):
        yield "ascii", m.group().decode("ascii", "replace")
    for m in re.finditer(rb"(?:[\x20-\x7e]|[\xe4-\xe9][\x80-\xbf]{2}){2,}", blob):
        raw = m.group()
        try:
            s = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if cjk.search(s) and len(s) >= min_len:
            yield "cjk", s


def main():
    asm, out_path = sys.argv[1], sys.argv[2]
    data = open(asm, "rb").read()
    secs = sections(data)
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    cli = None
    # CLI header offset comes from data directory 14 of the optional header.
    dd_off = pe + 24 + (96 if struct.unpack_from("<H", data, pe + 24)[0] == 0x10B else 112) + 14 * 8
    cli_rva = struct.unpack_from("<I", data, dd_off)[0]
    if cli_rva:
        cli = rva_to_offset(secs, cli_rva)
    resources_rva, resources_size = 0, 0
    if cli:
        resources_rva, resources_size = struct.unpack_from("<II", data, cli + 24)

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(f"# assembly: {asm}\n")
        fh.write(f"# cli_header_file_offset: {cli}\n")
        fh.write(f"# resources_rva: {resources_rva} size: {resources_size}\n")
        fh.write(f"# sections: {secs}\n\n")

        blob_off = rva_to_offset(secs, resources_rva) if resources_rva else None
        if blob_off is None:
            fh.write("!! could not map resource blob\n")
            blob = b""
        else:
            blob = data[blob_off:blob_off + resources_size]
            fh.write(f"# resource blob file offset: {blob_off}\n\n")
            fh.write("===== RESOURCE BLOB STRINGS =====\n")
            for kind, s in text_runs(blob):
                fh.write(f"[{kind}] {s}\n")

        fh.write("\n===== WHOLE FILE CJK =====\n")
        for kind, s in text_runs(data):
            if kind == "cjk":
                fh.write(f"[{kind}] {s}\n")


if __name__ == "__main__":
    main()
