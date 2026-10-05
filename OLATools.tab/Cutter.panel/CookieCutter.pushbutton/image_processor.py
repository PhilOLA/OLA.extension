"""
image_processor.py
CPython 3.x helper — parses an SVG file and outputs line segments as JSON.
Uses Python's built-in xml.etree — no pip dependencies needed.

Usage:
    python image_processor.py \
        --image "path/to/file.svg" \
        --height_mm 190 \
        --min_detail 0.5 \
        --output "path/to/output.json"

--smoothing and --tolerance are accepted but unused (kept for CLI compat).
"""

import argparse
import json
import math
import sys
import re
import xml.etree.ElementTree as ET


# ---------------------------------------------------------------------------
# SVG TRANSFORM HELPERS
# ---------------------------------------------------------------------------

def parse_transform(t):
    if not t:
        return [1,0,0,1,0,0]
    m = re.search(r'matrix\(([^)]+)\)', t)
    if m:
        v = [float(x) for x in re.split(r'[\s,]+', m.group(1).strip())]
        if len(v) == 6:
            return v
    m = re.search(r'translate\(([^)]+)\)', t)
    if m:
        v = [float(x) for x in re.split(r'[\s,]+', m.group(1).strip())]
        return [1,0,0,1, v[0], v[1] if len(v)>1 else 0]
    m = re.search(r'scale\(([^)]+)\)', t)
    if m:
        v = [float(x) for x in re.split(r'[\s,]+', m.group(1).strip())]
        sx = v[0]; sy = v[1] if len(v)>1 else sx
        return [sx,0,0,sy,0,0]
    return [1,0,0,1,0,0]

def mul_mat(a, b):
    return [
        a[0]*b[0]+a[2]*b[1], a[1]*b[0]+a[3]*b[1],
        a[0]*b[2]+a[2]*b[3], a[1]*b[2]+a[3]*b[3],
        a[0]*b[4]+a[2]*b[5]+a[4], a[1]*b[4]+a[3]*b[5]+a[5]
    ]

def apply_mat(m, x, y):
    return m[0]*x + m[2]*y + m[4], m[1]*x + m[3]*y + m[5]


# ---------------------------------------------------------------------------
# PATH TOKENISER
# ---------------------------------------------------------------------------

def tokenise(d):
    """Split path d into list of (CMD, [floats])."""
    tokens = re.findall(
        r'[MmZzLlHhVvCcSsQqTtAa]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?',
        d
    )
    out, cmd, args = [], None, []
    for tok in tokens:
        if tok.isalpha():
            if cmd is not None:
                out.append((cmd, args))
            cmd, args = tok, []
        else:
            args.append(float(tok))
    if cmd is not None:
        out.append((cmd, args))
    return out


# ---------------------------------------------------------------------------
# CURVE SAMPLERS
# ---------------------------------------------------------------------------

def sample_cubic(p0, p1, p2, p3, steps=20):
    pts = []
    for i in range(1, steps+1):
        t = i/steps; u = 1-t
        x = u**3*p0[0]+3*u**2*t*p1[0]+3*u*t**2*p2[0]+t**3*p3[0]
        y = u**3*p0[1]+3*u**2*t*p1[1]+3*u*t**2*p2[1]+t**3*p3[1]
        pts.append((x,y))
    return pts

def sample_quadratic(p0, p1, p2, steps=12):
    pts = []
    for i in range(1, steps+1):
        t = i/steps; u = 1-t
        x = u**2*p0[0]+2*u*t*p1[0]+t**2*p2[0]
        y = u**2*p0[1]+2*u*t*p1[1]+t**2*p2[1]
        pts.append((x,y))
    return pts

def sample_arc(x1,y1,rx,ry,x_rot,large,sweep,x2,y2,steps=20):
    if rx==0 or ry==0:
        return [(x2,y2)]
    phi = math.radians(x_rot)
    cp,sp = math.cos(phi), math.sin(phi)
    dx,dy = (x1-x2)/2, (y1-y2)/2
    x1p =  cp*dx + sp*dy
    y1p = -sp*dx + cp*dy
    rx,ry = abs(rx),abs(ry)
    lam = x1p**2/rx**2 + y1p**2/ry**2
    if lam>1: rx*=math.sqrt(lam); ry*=math.sqrt(lam)
    num = rx**2*ry**2 - rx**2*y1p**2 - ry**2*x1p**2
    den = rx**2*y1p**2 + ry**2*x1p**2
    sq  = math.sqrt(max(0,num/den)) * (-1 if large==sweep else 1)
    cxp =  sq*rx*y1p/ry
    cyp = -sq*ry*x1p/rx
    cx  = cp*cxp - sp*cyp + (x1+x2)/2
    cy  = sp*cxp + cp*cyp + (y1+y2)/2
    def ang(ux,uy,vx,vy):
        n=math.sqrt(ux**2+uy**2)*math.sqrt(vx**2+vy**2)
        if n==0: return 0
        a=math.acos(max(-1,min(1,(ux*vx+uy*vy)/n)))
        return -a if ux*vy-uy*vx<0 else a
    t1 = ang(1,0,(x1p-cxp)/rx,(y1p-cyp)/ry)
    dt = ang((x1p-cxp)/rx,(y1p-cyp)/ry,(-x1p-cxp)/rx,(-y1p-cyp)/ry)
    if not sweep and dt>0: dt-=2*math.pi
    elif sweep and dt<0:   dt+=2*math.pi
    pts=[]
    for i in range(1,steps+1):
        th = t1 + i/steps*dt
        x  = cp*rx*math.cos(th) - sp*ry*math.sin(th) + cx
        y  = sp*rx*math.cos(th) + cp*ry*math.sin(th) + cy
        pts.append((x,y))
    return pts


# ---------------------------------------------------------------------------
# PATH → POLYLINES
# ---------------------------------------------------------------------------

def path_to_polylines(d):
    """
    Parse SVG path d string into a list of closed polylines.
    Each polyline is a list of (x,y) tuples in SVG coordinates.
    """
    cmds = tokenise(d)
    polys, cur, cx, cy, sx, sy = [], [], 0.0, 0.0, 0.0, 0.0
    last_cmd, last_cp = None, None

    def add(x, y):
        if not cur or (abs(cur[-1][0]-x)>1e-9 or abs(cur[-1][1]-y)>1e-9):
            cur.append((x,y))

    for cmd, args in cmds:
        C = cmd.upper()
        rel = cmd.islower()

        def r(x, y=None, base_x=None, base_y=None):
            """Resolve relative coords."""
            bx = base_x if base_x is not None else cx
            by = base_y if base_y is not None else cy
            if y is None:
                return (x+bx) if rel else x
            return (x+bx if rel else x), (y+by if rel else y)

        if C == 'M':
            if cur:
                polys.append(list(cur))
                cur = []
            pairs = [(args[i],args[i+1]) for i in range(0,len(args)-1,2)]
            for i,(x,y) in enumerate(pairs):
                nx,ny = (x+cx,y+cy) if rel else (x,y)
                if i==0:
                    cx,cy,sx,sy = nx,ny,nx,ny
                    add(cx,cy)
                else:
                    cx,cy = nx,ny
                    add(cx,cy)
            last_cmd,last_cp = C, None

        elif C == 'Z':
            add(sx,sy)
            if cur:
                polys.append(list(cur))
                cur = []
            cx,cy = sx,sy
            last_cmd,last_cp = C, None

        elif C == 'L':
            pairs = [(args[i],args[i+1]) for i in range(0,len(args)-1,2)]
            for x,y in pairs:
                cx,cy = (x+cx,y+cy) if rel else (x,y)
                add(cx,cy)
            last_cmd,last_cp = C, None

        elif C == 'H':
            for x in args:
                cx = (x+cx) if rel else x
                add(cx,cy)
            last_cmd,last_cp = C, None

        elif C == 'V':
            for y in args:
                cy = (y+cy) if rel else y
                add(cx,cy)
            last_cmd,last_cp = C, None

        elif C == 'C':
            i=0
            while i+5 < len(args)+1:
                x1,y1,x2,y2,x,y = args[i:i+6]
                if rel: x1+=cx;y1+=cy;x2+=cx;y2+=cy;x+=cx;y+=cy
                for p in sample_cubic((cx,cy),(x1,y1),(x2,y2),(x,y)):
                    add(*p)
                last_cp=(x2,y2); cx,cy=x,y
                i+=6
            last_cmd = C

        elif C == 'S':
            i=0
            while i+3 < len(args)+1:
                x2,y2,x,y = args[i:i+4]
                if rel: x2+=cx;y2+=cy;x+=cx;y+=cy
                x1 = 2*cx-last_cp[0] if last_cp and last_cmd in ('C','S') else cx
                y1 = 2*cy-last_cp[1] if last_cp and last_cmd in ('C','S') else cy
                for p in sample_cubic((cx,cy),(x1,y1),(x2,y2),(x,y)):
                    add(*p)
                last_cp=(x2,y2); cx,cy=x,y
                i+=4
            last_cmd = C

        elif C == 'Q':
            i=0
            while i+3 < len(args)+1:
                x1,y1,x,y = args[i:i+4]
                if rel: x1+=cx;y1+=cy;x+=cx;y+=cy
                for p in sample_quadratic((cx,cy),(x1,y1),(x,y)):
                    add(*p)
                last_cp=(x1,y1); cx,cy=x,y
                i+=4
            last_cmd = C

        elif C == 'T':
            i=0
            while i+1 < len(args)+1:
                x,y = args[i:i+2]
                if rel: x+=cx;y+=cy
                x1 = 2*cx-last_cp[0] if last_cp and last_cmd in ('Q','T') else cx
                y1 = 2*cy-last_cp[1] if last_cp and last_cmd in ('Q','T') else cy
                for p in sample_quadratic((cx,cy),(x1,y1),(x,y)):
                    add(*p)
                last_cp=(x1,y1); cx,cy=x,y
                i+=2
            last_cmd = C

        elif C == 'A':
            i=0
            while i+6 < len(args)+1:
                rx,ry,xr,la,sw,x,y = args[i:i+7]
                if rel: x+=cx;y+=cy
                for p in sample_arc(cx,cy,rx,ry,xr,int(la),int(sw),x,y):
                    add(*p)
                cx,cy=x,y; last_cp=None
                i+=7
            last_cmd = C

    if cur:
        polys.append(cur)
    return polys


# ---------------------------------------------------------------------------
# COLLECT PATHS FROM SVG TREE
# ---------------------------------------------------------------------------

def collect_paths(el, parent_mat=None):
    if parent_mat is None:
        parent_mat = [1,0,0,1,0,0]
    own  = parse_transform(el.get('transform',''))
    mat  = mul_mat(parent_mat, own)
    tag  = el.tag.split('}')[-1] if '}' in el.tag else el.tag
    res  = []
    if tag == 'path':
        d = el.get('d','')
        if d:
            res.append((d, mat))
    for child in el:
        res.extend(collect_paths(child, mat))
    return res


# ---------------------------------------------------------------------------
# SCALE & FLIP TO MM
# ---------------------------------------------------------------------------

def scale_to_mm(polyline, target_height_mm):
    """
    Scale a single polyline so its bounding box height = target_height_mm.
    Centre on origin, flip Y (SVG Y-down → Revit Y-up).
    """
    xs = [p[0] for p in polyline]
    ys = [p[1] for p in polyline]
    min_x,max_x = min(xs),max(xs)
    min_y,max_y = min(ys),max(ys)
    h = max_y - min_y
    if h == 0:
        return polyline
    scale = target_height_mm / h
    cx = (min_x+max_x)/2
    cy = (min_y+max_y)/2
    return [((p[0]-cx)*scale, -(p[1]-cy)*scale) for p in polyline]


# ---------------------------------------------------------------------------
# POLYLINE → LINE SEGMENTS
# ---------------------------------------------------------------------------

def to_segments(points, min_len_mm=0.8):
    segs = []
    if len(points) < 2:
        return segs
    prev = points[0]
    for pt in points[1:]:
        d = math.hypot(pt[0]-prev[0], pt[1]-prev[1])
        if d >= min_len_mm:
            segs.append({"type":"line","start":list(prev),"end":list(pt)})
            prev = pt
    # Close loop
    d = math.hypot(points[0][0]-prev[0], points[0][1]-prev[1])
    if d >= min_len_mm:
        segs.append({"type":"line","start":list(prev),"end":list(points[0])})
    return segs


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image",      required=True)
    ap.add_argument("--height_mm",  type=float, default=190.0)
    ap.add_argument("--smoothing",  type=float, default=5.0)
    ap.add_argument("--min_detail", type=float, default=0.8)
    ap.add_argument("--tolerance",  type=float, default=0.5)
    ap.add_argument("--output",     required=True)
    args = ap.parse_args()

    try:
        tree = ET.parse(args.image)
        root = tree.getroot()

        path_data = collect_paths(root)
        if not path_data:
            raise ValueError("No <path> elements found in SVG.")

        # Parse all paths, apply transforms
        all_polylines = []
        for d, mat in path_data:
            for pl in path_to_polylines(d):
                if len(pl) < 3:
                    continue
                # Apply matrix transform to each point
                tpl = [apply_mat(mat, x, y) for x,y in pl]
                xs = [p[0] for p in tpl]
                ys = [p[1] for p in tpl]
                area = (max(xs)-min(xs)) * (max(ys)-min(ys))
                all_polylines.append((area, tpl))

        if not all_polylines:
            raise ValueError("No valid polylines found in SVG.")

        # Pick the largest polyline by bounding box area — outer contour
        all_polylines.sort(key=lambda x: x[0], reverse=True)
        outer = all_polylines[0][1]

        # Scale to mm with Y flip
        scaled = scale_to_mm(outer, args.height_mm)

        # Convert to segments, minimum length = max(min_detail, 0.8mm)
        min_len = max(0.8, args.min_detail)
        segs = to_segments(scaled, min_len_mm=min_len)

        if not segs:
            raise ValueError("No segments generated. Try reducing min_detail.")

        # Write as simple CSV: sx,sy,ex,ey per line
        # Avoids IronPython 2.7 JSON parser limitations with large files
        with open(args.output, "w") as f:
            f.write("count:{}\n".format(len(segs)))
            for s in segs:
                f.write("{},{},{},{}\n".format(
                    s["start"][0], s["start"][1],
                    s["end"][0],   s["end"][1]
                ))

        print(json.dumps({
            "success": True,
            "segment_count": len(segs),
            "polylines_found": len(all_polylines)
        }))

    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

if __name__ == "__main__":
    main()
