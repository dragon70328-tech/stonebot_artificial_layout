import zipfile, re, json, sys
from xml.etree import ElementTree as ET
p = r"C:\Users\drago\Desktop\临时文件\用量计算.xlsx"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
z = zipfile.ZipFile(p)
ss = z.read("xl/sharedStrings.xml")
root = ET.fromstring(ss)
shared = []
for si in root.findall(NS+"si"):
    texts = si.findall(NS+"t")
    shared.append("".join(t.text or "" for t in texts))
sheet = z.read("xl/worksheets/sheet1.xml")
sroot = ET.fromstring(sheet)
rows = []
for row in sroot.iter(NS+"row"):
    vals = {}
    for c in row.findall(NS+"c"):
        ref = c.get("r")
        col = re.match(r"[A-Z]+", ref).group(0)
        t = c.get("t")
        v = c.find(NS+"v")
        istr = c.find(NS+"is")
        if t == "s" and v is not None:
            val = shared[int(v.text)]
        elif t == "inlineStr" and istr is not None:
            val = "".join(x.text or "" for x in istr.findall(NS+"t"))
        elif v is not None:
            val = v.text
        else:
            val = ""
        vals[col] = val
    if vals:
        rows.append(vals)
print(json.dumps(rows, ensure_ascii=False, indent=2))
