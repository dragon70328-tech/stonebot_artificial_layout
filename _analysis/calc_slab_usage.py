import zipfile, re, json, math
from xml.etree import ElementTree as ET
from collections import defaultdict

EXCEL = r"C:\Users\drago\Desktop\临时文件\用量计算.xlsx"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_rows(path):
    z = zipfile.ZipFile(path)
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    shared = []
    for si in root.findall(NS + "si"):
        shared.append("".join((t.text or "") for t in si.findall(NS + "t")))
    sroot = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in sroot.iter(NS + "row"):
        vals = {}
        for c in row.findall(NS + "c"):
            ref = c.get("r")
            if not ref:
                continue
            col = re.match(r"[A-Z]+", ref).group(0)
            t = c.get("t")
            v = c.find(NS + "v")
            istr = c.find(NS + "is")
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif t == "inlineStr" and istr is not None:
                val = "".join(x.text or "" for x in istr.findall(NS + "t"))
            elif v is not None:
                val = v.text
            else:
                val = ""
            vals[col] = val
        if vals:
            rows.append(vals)
    return rows


def ffd_1d(items, capacity):
    bins = []
    for length in sorted(items, reverse=True):
        placed = False
        for b in bins:
            if sum(b) + length <= capacity + 1e-9:
                b.append(length)
                placed = True
                break
        if not placed:
            bins.append([length])
    return bins


def bfd_1d(items, capacity):
    bins = []
    for length in sorted(items, reverse=True):
        best = None
        best_rem = None
        for b in bins:
            rem = capacity - sum(b)
            if rem + 1e-9 >= length:
                if best is None or rem < best_rem:
                    best = b
                    best_rem = rem
        if best is not None:
            best.append(length)
        else:
            bins.append([length])
    return bins


def main():
    rows = read_rows(EXCEL)
    header = rows[0]
    data = rows[1:]
    print("HEADER", header)
    print("DATA_ROWS", len(data))

    materials = defaultdict(lambda: {"rows": [], "pieces": 0, "area": 0.0})
    for r in data:
        material = r.get("A", "").strip()
        length = float(r.get("C", "0"))
        width = float(r.get("D", "0"))
        qty = int(float(r.get("E", "0")))
        materials[material]["rows"].append((length, width, qty))
        materials[material]["pieces"] += qty
        materials[material]["area"] += length * width * qty / 1e6

    small_slab = (2400.0, 1200.0)
    big_slab = (2500.0, 1400.0)
    results = {}
    for material, info in sorted(materials.items()):
        small_items = []
        big_qty = 0
        big_dim_pairs = []
        for length, width, qty in info["rows"]:
            min_dim = min(length, width)
            max_dim = max(length, width)
            if min_dim <= 1200.0 and max_dim <= 2400.0:
                item_length = min_dim if max_dim <= 1200.0 else max_dim
                small_items.extend([item_length] * qty)
            else:
                big_qty += qty
                big_dim_pairs.extend([(length, width)] * qty)
        from collections import Counter
        if material in ("ST-101", "ST-102", "ST-103", "ST-104"):
            dist = Counter()
            for length, width, qty in info["rows"]:
                min_dim = min(length, width); max_dim = max(length, width)
                if min_dim <= 1200.0 and max_dim <= 2400.0:
                    item_length = min_dim if max_dim <= 1200.0 else max_dim
                    dist[item_length] += qty
                else:
                    dist[("big", length, width)] += qty
            print("DIST", material, dict(sorted(dist.items(), key=lambda x: str(x[0]))))
        small_bins = ffd_1d(small_items, 2400.0)
        small_bins_bfd = bfd_1d(small_items, 2400.0)

        paired_small_items = small_items[:]
        paired_count = 0
        for item in sorted(paired_small_items, reverse=True):
            if paired_count >= big_qty:
                break
            if item <= 1253.0:
                paired_small_items.remove(item)
                paired_count += 1
        paired_bins = ffd_1d(paired_small_items, 2400.0)
        paired_bins_bfd = bfd_1d(paired_small_items, 2400.0)
        paired_sum = sum(paired_small_items)

        results[material] = {
            "pieces": info["pieces"],
            "area_m2": round(info["area"], 3),
            "small_items": len(small_items),
            "big_items": big_qty,
            "small_boards_2400x1200": len(small_bins),
            "small_boards_2400x1200_bfd": len(small_bins_bfd),
            "big_boards_2500x1400": big_qty,
            "paired_small_boards_2400x1200": len(paired_bins),
            "paired_small_boards_2400x1200_bfd": len(paired_bins_bfd),
            "paired_count": paired_count,
            "paired_length_lower_bound": math.ceil(paired_sum / 2400.0),
        }
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print("\nFINAL_PAIRED_TABLE")
    print(f"{'material':10} {'pieces':>7} {'area_m2':>9} {'2400x1200':>9} {'2500x1400':>9} {'board_area':>10} {'util%':>7}")
    for material, r in results.items():
        small = r["paired_small_boards_2400x1200"]
        big = r["big_boards_2500x1400"]
        board_area = small * 2.88 + big * 3.5
        util = r["area_m2"] / board_area * 100
        print(f"{material:10} {r['pieces']:7} {r['area_m2']:9.3f} {small:9} {big:9} {board_area:10.2f} {util:6.1f}%")


if __name__ == "__main__":
    main()
