# -*- coding: utf-8 -*-
"""v5：门（框内斜线）排除 + 壁龛单独列出 + 壁龛所在大板开孔方案。"""
from __future__ import annotations
import collections, csv, math, statistics
from pathlib import Path
import ezdxf
from shapely.geometry import LineString, Point, box
from shapely.ops import polygonize, unary_union

SRC = r"C:\Users\drago\Desktop\临时文件\AI识图文件\悦府顶层43-44层.dxf"
OUT_DIR = Path(r"C:\Users\drago\Desktop\临时文件\AI识图文件\_审图_悦府顶层")
OUT_DIR.mkdir(parents=True, exist_ok=True)

doc = ezdxf.readfile(SRC); msp = doc.modelspace()
titles, materials, segs = [], [], []
for e in msp:
    if e.dxftype() == "TEXT":
        t = str(e.dxf.text).strip()
        if "材料：" in t or "材料:" in t:
            materials.append((t, e.dxf.insert.x, e.dxf.insert.y))
        elif ("立面" in t or "背景墙" in t or "造型墙面" in t) and "平面" not in t:
            titles.append((t, e.dxf.insert.x, e.dxf.insert.y))
    pts = []
    if e.dxftype() == "LINE":
        pts = [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
    elif e.dxftype() == "LWPOLYLINE":
        pts = [(float(p[0]), float(p[1])) for p in e.get_points()]
        if e.closed and len(pts) > 2: pts.append(pts[0])
    elif e.dxftype() == "ARC":
        c, r = e.dxf.center, e.dxf.radius
        a0 = math.radians(e.dxf.start_angle); a1 = math.radians(e.dxf.end_angle)
        if a1 < a0: a1 += 2*math.pi
        pts = [(c.x+r*math.cos(a0+(a1-a0)*i/24), c.y+r*math.sin(a0+(a1-a0)*i/24)) for i in range(25)]
    for i in range(len(pts)-1):
        a,b = pts[i],pts[i+1]
        qa=(round(a[0]),round(a[1])); qb=(round(b[0]),round(b[1]))
        if qa != qb: segs.append((qa[0],qa[1],qb[0],qb[1]))

# 抽槽打线
vg = collections.defaultdict(list)
for x0,y0,x1,y1 in segs:
    if abs(x0-x1) < 0.6 and abs(y0-y1) > 200:
        vg[(min(y0,y1),max(y0,y1))].append(x0)
hatch = []
for (y0,y1), xs in vg.items():
    xs = sorted(xs)
    if len(xs) < 5: continue
    gaps=[b-a for a,b in zip(xs,xs[1:]) if b-a>0.5]
    if gaps and statistics.median(gaps) <= 50:
        hatch.append((xs[0],y0,xs[-1],y1))

def in_hatch(s):
    x0,y0,x1,y1 = s
    if abs(x0-x1)>=0.6 or abs(y0-y1)<=200: return False
    yy0,yy1 = min(y0,y1),max(y0,y1)
    return any(hx0-1<=x0<=hx1+1 and hy0-1<=yy0 and yy1<=hy1+1 for hx0,hy0,hx1,hy1 in hatch)

clean = [s for s in segs if not in_hatch(s)]
faces = [f for f in polygonize(unary_union([LineString([(s[0],s[1]),(s[2],s[3])]) for s in clean]))
         if 20000 <= f.area <= 3.2e6
         and min(f.bounds[2]-f.bounds[0], f.bounds[3]-f.bounds[1]) >= 50
         and max(f.bounds[2]-f.bounds[0], f.bounds[3]-f.bounds[1]) /
             max(min(f.bounds[2]-f.bounds[0], f.bounds[3]-f.bounds[1]), 1e-9) <= 8]

# 门：一个矩形被对角线切成两个三角形 —— 同包围盒的两块三角面合成一个门
from shapely.geometry import box as _box
by_bbox = collections.defaultdict(list)
for f in faces:
    b = f.bounds
    key = (round(b[0]/2)*2, round(b[1]/2)*2, round(b[2]/2)*2, round(b[3]/2)*2)
    by_bbox[key].append(f)

DOORS = set()
DOOR_RECTS = []

# A. 单个三角面：三个顶点都在包围盒角上 → 门框的一半
for f in faces:
    coords = list(f.exterior.coords)[:-1]
    if len(coords) != 3:
        continue
    b = f.bounds
    corners = [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]
    def _on_corner(p):
        return any(abs(p[0]-c[0]) <= 2 and abs(p[1]-c[1]) <= 2 for c in corners)
    if all(_on_corner(p) for p in coords):
        DOORS.add(id(f))
        DOOR_RECTS.append((round(b[0]), round(b[1]), round(b[2]), round(b[3])))

# B. 同包围盒的两块三角面
for key, items in by_bbox.items():
    if len(items) != 2:
        continue
    x0, y0, x1, y1 = key
    bbox_area = (x1-x0) * (y1-y0)
    if bbox_area <= 0:
        continue
    if all(len(f.exterior.coords) == 4 for f in items) and             abs(sum(f.area for f in items) - bbox_area) <= 0.01 * bbox_area:
        DOORS.update(id(f) for f in items)
        DOOR_RECTS.append((x0, y0, x1, y1))
print("门（对角线矩形）:", len(DOOR_RECTS))

# 壁龛：>=4 个同尺寸小面，集中在一处
by_size = collections.defaultdict(list)
for f in faces:
    b=f.bounds
    by_size[(round((b[2]-b[0])/2)*2, round((b[3]-b[1])/2)*2)].append(f)
NICHES = set()
niche_groups = []
for (w,h), items in by_size.items():
    if len(items) < 4 or w*h > 0.20e6: continue
    xs=[f.bounds[0] for f in items]+[f.bounds[2] for f in items]
    ys=[f.bounds[1] for f in items]+[f.bounds[3] for f in items]
    if max(xs)-min(xs) <= 2500 and max(ys)-min(ys) <= 3000:
        NICHES.update(id(f) for f in items)
        niche_groups.append(items)

def assign(f):
    c=f.centroid; best=None
    for name,tx,ty in titles:
        if ty+50 <= c.y <= ty+2800 and abs(c.x-tx) <= 2500:
            d=abs(c.x-tx)+0.2*abs(c.y-(ty+1200))
            if best is None or d<best[0]: best=(d,name)
    if best: return best[1]
    # 兜底：最近的标题（放宽到 4000mm），避免壁龛落在标题格之外被丢掉
    near=None
    for name,tx,ty in titles:
        d=math.hypot(c.x-tx, c.y-ty)
        if d<=4000 and (near is None or d<near[0]): near=(d,name)
    return near[1] if near else None

buckets = collections.defaultdict(list)
for f in faces:
    n = assign(f)
    if n: buckets[n].append(f)

def grooved(f):
    b=f.bounds
    return any(b[0]>=hx0-2 and b[2]<=hx1+2 and b[1]>=hy0-2 and b[3]<=hy1+2
               for hx0,hy0,hx1,hy1 in hatch)

out = ezdxf.new(); out.units=4
for name,color in (("ELEV",8),("OUTER",7),("HOLES",5),("NUMBERS",3),("GROOVES",1),
                   ("DOOR",9),("NICHE",30),("HOST_PROPOSED",4),("MATERIAL",6),
                   ("HATCH_MARK",4)):
    out.layers.add(name, color=color)
m = out.modelspace()
for hx0,hy0,hx1,hy1 in hatch:
    m.add_lwpolyline([(hx0,hy0),(hx1,hy0),(hx1,hy1),(hx0,hy1)], close=True,
                     dxfattribs={"layer":"HATCH_MARK"})

rows=[]; n_stone=n_door=n_niche=0; host_note=[]
for name,tx,ty in sorted(titles, key=lambda t:t[1]):
    fs = buckets.get(name, [])
    if not fs: continue
    cx=sum(f.centroid.x for f in fs)/len(fs); cy=sum(f.centroid.y for f in fs)/len(fs)
    mat = min(materials,key=lambda t:math.hypot(t[1]-cx,t[2]-cy))[0] if materials else ""
    bx=min(f.bounds[0] for f in fs); by=min(f.bounds[1] for f in fs)
    m.add_text(name,height=100,dxfattribs={"layer":"ELEV","insert":(bx,by-300)})
    if mat: m.add_text(mat,height=80,dxfattribs={"layer":"MATERIAL","insert":(bx,by-450)})

    for f in sorted(fs,key=lambda f:(-f.bounds[3],f.bounds[0])):
        b=f.bounds; w,h=b[2]-b[0],b[3]-b[1]
        if id(f) in DOORS:
            n_door += 1
            rows.append({"编号":"D-%03d"%n_door,"类型":"门","立面":name,"材料":mat,
                         "长mm":round(max(w,h),1),"宽mm":round(min(w,h),1),
                         "面积m2":round(f.area/1e6,4),"抽槽":"","备注":"框内斜线=门，不做石材",
                         "X":round(b[0]),"Y":round(b[1])})
            continue
        if id(f) in NICHES:
            n_niche += 1
            label="N-%03d"%n_niche
            m.add_lwpolyline([(x,y) for x,y,*_ in f.exterior.coords],close=True,
                             dxfattribs={"layer":"NICHE"})
            m.add_text(label,height=40,dxfattribs={"layer":"NICHE",
                       "insert":(b[0]+w/2-30,b[1]+h/2-15)})
            rows.append({"编号":label,"类型":"壁龛","立面":name,"材料":mat,
                         "长mm":round(max(w,h),1),"宽mm":round(min(w,h),1),
                         "面积m2":round(f.area/1e6,4),"抽槽":"","备注":"壁龛用材单独列",
                         "X":round(b[0]),"Y":round(b[1])})
            continue
        n_stone += 1
        label="S-%03d"%n_stone
        rings=[f.exterior]+list(f.interiors)
        for k,ring in enumerate(rings):
            m.add_lwpolyline([(x,y) for x,y,*_ in ring.coords],close=True,
                             dxfattribs={"layer":"HOLES" if k else "OUTER"})
        is_g = grooved(f)
        m.add_text(label,height=min(60,max(25,min(w,h)/6)),
                   dxfattribs={"layer":"NUMBERS","insert":(b[0]+w/2-40,b[1]+h/2-20)})
        if is_g:
            m.add_text("5x5抽槽见光",height=min(45,max(20,min(w,h)/10)),
                       dxfattribs={"layer":"GROOVES","insert":(b[0]+20,b[1]+20)})
        rows.append({"编号":label,"类型":"石材板","立面":name,"材料":mat,
                     "长mm":round(max(w,h),1),"宽mm":round(min(w,h),1),
                     "面积m2":round(f.area/1e6,4),"抽槽":"是" if is_g else "",
                     "备注":"","X":round(b[0]),"Y":round(b[1])})

# 壁龛所在大板：按邻近竖/横线取包围矩形，扣除壁龛格
for gi, items in enumerate(niche_groups, 1):
    xs0=min(f.bounds[0] for f in items); xs1=max(f.bounds[2] for f in items)
    ys0=min(f.bounds[1] for f in items); ys1=max(f.bounds[3] for f in items)
    vclose=[x0 for x0,y0,x1,y1 in segs if abs(x0-x1)<1 and x0<xs0 and x0>=xs0-900]
    vclose2=[x0 for x0,y0,x1,y1 in segs if abs(x0-x1)<1 and x0>xs1 and x0<=xs1+900]
    hclose=[y0 for x0,y0,x1,y1 in segs if abs(y0-y1)<1 and (y0<ys0 and y0>=ys0-600)]
    hclose2=[y0 for x0,y0,x1,y1 in segs if abs(y0-y1)<1 and (y0>ys1 and y0<=ys1+600)]
    hx0=min(vclose) if vclose else xs0; hx1=max(vclose2) if vclose2 else xs1
    hy0=min(hclose) if hclose else ys0; hy1=max(hclose2) if hclose2 else ys1
    host = box(hx0,hy0,hx1,hy1)
    for f in items: host = host.difference(f)
    m.add_lwpolyline([(hx0,hy0),(hx1,hy0),(hx1,hy1),(hx0,hy1)], close=True,
                     dxfattribs={"layer":"HOST_PROPOSED"})
    for ring in [host.exterior]+list(host.interiors):
        m.add_lwpolyline([(x,y) for x,y,*_ in ring.coords],close=True,
                         dxfattribs={"layer":"HOST_PROPOSED"})
    host_note.append((gi,len(items),hx1-hx0,hy1-hy0))

for x0, y0, x1, y1 in DOOR_RECTS:
    m.add_lwpolyline([(x0,y0),(x1,y0),(x1,y1),(x0,y1)], close=True,
                     dxfattribs={"layer":"DOOR"})
    m.add_text("门", height=60,
               dxfattribs={"layer":"DOOR","insert":((x0+x1)/2-30,(y0+y1)/2-20)})

dxf_path = OUT_DIR/"悦府顶层_立面石材_原位_v2.dxf"
out.saveas(str(dxf_path))
csv_path = OUT_DIR/"悦府顶层_立面石材_清单_v2.csv"
with csv_path.open("w",encoding="utf-8-sig",newline="") as fh:
    wr=csv.DictWriter(fh,fieldnames=["编号","类型","立面","材料","长mm","宽mm","面积m2","抽槽","备注","X","Y"])
    wr.writeheader(); wr.writerows(rows)

print("石材板 %d / 壁龛 %d / 门 %d" % (n_stone, n_niche, n_door))
print("石材面积 %.2f m2  壁龛面积 %.2f m2" % (
    sum(r["面积m2"] for r in rows if r["类型"]=="石材板"),
    sum(r["面积m2"] for r in rows if r["类型"]=="壁龛")))
print("壁龛所在大板（拟定）:", host_note)
print("DXF:", dxf_path)