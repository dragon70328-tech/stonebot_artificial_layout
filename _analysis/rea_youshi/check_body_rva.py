"""Diagnose method-body RVAs in a .NET assembly whose bodies fail to decompile.

Checks whether the recorded RVAs are byte-shifted relative to plausible CLI
method headers (tiny header: byte & 3 == 2; fat header: byte & 3 == 3).

Usage:
    python -X utf8 _analysis/rea_youshi/check_body_rva.py <assembly.dll> <rva> [rva ...]
"""

import os
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


def r2o(secs, rva):
    for _n, vaddr, vsize, raw_ptr, raw_size in secs:
        if vaddr <= rva < vaddr + max(vsize, raw_size):
            return raw_ptr + (rva - vaddr)
    return None


def header_kind(byte):
    low = byte & 3
    if low == 2:
        return f"tiny header, code size {byte >> 2}"
    if low == 3:
        return "fat header"
    return "not a header"


def main():
    asm = sys.argv[1]
    data = open(asm, "rb").read()
    secs = sections(data)
    print("sections:", secs)
    for rva_txt in sys.argv[2:]:
        rva = int(rva_txt, 0)
        off = r2o(secs, rva)
        print(f"\nRVA 0x{rva:X} -> file offset {off} (rva % 4 = {rva % 4})")
        if off is None:
            continue
        for delta in (-4, -3, -2, -1, 0, 1, 2, 3, 4):
            b = data[off + delta]
            print(f"   rva{delta:+d}: byte 0x{b:02X}  {header_kind(b)}   ctx={data[off+delta:off+delta+8].hex()}")


if __name__ == "__main__":
    main()
