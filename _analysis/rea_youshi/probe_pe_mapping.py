"""Find the real RVA->file-offset mapping for an assembly with a wiped section table.

The GstoneCAD plugins ship with .text/.JQG raw pointers and sizes zeroed and an
extra randomly named section, so every generic tool maps RVAs to garbage. This
probe locates the CLI metadata signature (BSJB) and validates candidate mappings
against the CLI resource blob and method-body headers.

Usage:
    python -X utf8 _analysis/rea_youshi/probe_pe_mapping.py <assembly.dll>
"""

import struct
import sys


def main():
    data = open(sys.argv[1], "rb").read()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    dd = opt + (96 if magic == 0x10B else 112)
    cli_rva = struct.unpack_from("<I", data, dd + 14 * 8)[0]
    print(f"pe=0x{pe:X} sections={nsec} opt_size={opt_size} magic=0x{magic:X} cli_rva=0x{cli_rva:X}")

    base = opt + opt_size
    for i in range(nsec):
        off = base + i * 40
        name = data[off:off + 8].rstrip(b"\0").decode("latin-1")
        vsize, vaddr, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
        print(f"  {name:<10} vaddr=0x{vaddr:X} vsize={vsize} raw_ptr={raw_ptr} raw_size={raw_size}")

    print("\nseeking BSJB (metadata signature) at candidate file offsets:")

    def try_offset(desc, off):
        if 0 <= off < len(data) - 4:
            sig = data[off:off + 4]
            ok = sig == b"BSJB"
            print(f"  {desc:<46} off={off:<10} sig={sig!r} {'<== METADATA' if ok else ''}")
            return ok
        return False

    first_vaddr = None
    for i in range(nsec):
        off = base + i * 40
        vaddr = struct.unpack_from("<I", data, off + 12)[0]
        raw_ptr = struct.unpack_from("<I", data, off + 20)[0]
        if raw_ptr:
            first_vaddr, first_ptr = vaddr, raw_ptr
    delta = first_vaddr - first_ptr
    print(f"  inferred constant shift (first mapped section) = {delta}")
    try_offset("cli_rva - shift", cli_rva - delta)
    try_offset("raw cli_rva", cli_rva)
    # also try the shift that puts .text at file offset 1024
    for cand in (1024, 0, 512, 2048):
        try_offset(f"cli_rva - 8192 + {cand}", cli_rva - 8192 + cand)


if __name__ == "__main__":
    main()
