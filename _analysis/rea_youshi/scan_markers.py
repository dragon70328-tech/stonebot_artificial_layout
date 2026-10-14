"""Scan the GstoneCAD artifacts for protector/obfuscator markers and PE exports.

Static byte/PE scanning only; nothing is loaded or executed.

Usage:
    python -X utf8 _analysis/rea_youshi/scan_markers.py
"""

import glob
import os
import struct

ROOT = os.path.dirname(os.path.abspath(__file__))

MARKERS = [
    "Eazfuscator", "Confuser", "ObfuscationAttribute", "Agile.NET", ".NET Reactor",
    "Dotfuscator", "Themida", "SmartAssembly", "Babel", "DeepSea", "Goliath",
    "xenocode", "VMProtect", "Spices.Net", "Rummage", "CodeVeil", "Phoenix",
    "PackAttribute", "NeoImageAttribute",
]


def count_both(data, name):
    return data.count(name.encode("utf-8")) + data.count(name.encode("utf-16-le"))


def pe_exports(data):
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return None
    machine = struct.unpack_from("<H", data, pe + 4)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    dd = opt + (96 if magic == 0x10B else 112)
    exp_rva, exp_size = struct.unpack_from("<II", data, dd)
    if not exp_rva:
        return machine, []
    # section table for rva->offset
    base = opt + opt_size
    secs = []
    for i in range(nsec):
        off = base + i * 40
        vsize, vaddr, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
        secs.append((vaddr, vsize, raw_ptr, raw_size))

    def r2o(rva):
        for vaddr, vsize, raw_ptr, raw_size in secs:
            if vaddr <= rva < vaddr + max(vsize, raw_size):
                return raw_ptr + (rva - vaddr)
        return None

    eo = r2o(exp_rva)
    if eo is None:
        return machine, []
    nnames, nfuncs = struct.unpack_from("<II", data, eo + 20)[0], struct.unpack_from("<I", data, eo + 24)[0]
    names = []
    for i in range(nnames):
        nrva = struct.unpack_from("<I", data, eo + 32 + i * 4)[0]
        no = r2o(nrva)
        if no is None:
            continue
        end = data.index(b"\0", no)
        names.append(data[no:end].decode("ascii", "replace"))
    return machine, names


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "targets", "*")))
    for path in files:
        if os.path.splitext(path)[1].lower() not in (".dll", ".exe"):
            continue
        data = open(path, "rb").read()
        hits = [f"{m}:{c}" for m in MARKERS if (c := count_both(data, m))]
        line = f"{os.path.basename(path):<40} {len(data):>9}  {' '.join(hits) if hits else '-'}"
        print(line)
        exp = pe_exports(data)
        if exp:
            machine, names = exp
            if names:
                print(f"    exports({len(names)}): {', '.join(names[:40])}")


if __name__ == "__main__":
    main()
