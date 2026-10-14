"""Summarize the REA evidence JSON dumped for the GstoneCAD artifacts.

Reads ident_*.json and members_*.json written by dump_evidence.ps1 and prints a
compact inventory: managed identity, architecture, references, sizes, and where
member-level evidence was available. Static evidence only.

Usage:
    python -X utf8 _analysis/rea_youshi/summarize.py
"""

import glob
import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))


def load(path):
    return json.load(open(path, encoding="utf-8-sig"))


def main():
    print("=== identity ===")
    for path in sorted(glob.glob(os.path.join(ROOT, "ident_*.json"))):
        d = load(path)
        nr = d.get("normalized_result") or {}
        pe = nr.get("pe") or {}
        cli = pe.get("cli") or {}
        meta = nr.get("metadata") or {}
        asm = meta.get("assembly") or {}
        rows = meta.get("table_row_counts") or {}
        cls = nr.get("classification") or {}
        name = os.path.basename(path)[6:-5]
        print(
            f"{name:<38} {cls.get('status', '?'):<9} {str(pe.get('architecture')):<7} "
            f"asm={asm.get('name')} v{asm.get('version')} "
            f"TypeDef={rows.get('TypeDef')} MethodDef={rows.get('MethodDef')} "
            f"Res={rows.get('ManifestResource')} flags={cli.get('flag_names')}"
        )
        refs = [r.get("name") for r in (meta.get("references") or [])]
        if refs:
            print(f"    refs: {', '.join(refs)}")
        issues = (nr.get("coverage") or {}).get("issues") or []
        for i in issues[:2]:
            print(f"    coverage: {i.get('code')} {i.get('scope')} - {str(i.get('detail'))[:80]}")
        print(f"    evidence_id: {d.get('evidence_id')}")

    print("\n=== members availability ===")
    for path in sorted(glob.glob(os.path.join(ROOT, "members_*.json"))):
        d = load(path)
        nr = d.get("normalized_result") or {}
        types = nr.get("types") or []
        methods = nr.get("methods") or []
        name = os.path.basename(path)[8:-5]
        if types:
            print(
                f"{name:<38} OK       types={len(types)} methods={len(methods)} "
                f"call_edges={len(nr.get('call_edges') or [])} "
                f"evidence_id={d.get('evidence_id')}"
            )
        else:
            reasons = [i.get("detail") for i in (nr.get("coverage") or {}).get("issues") or []]
            print(f"{name:<38} BLOCKED  {(reasons[0] if reasons else 'unknown')[:70]}")


if __name__ == "__main__":
    main()
