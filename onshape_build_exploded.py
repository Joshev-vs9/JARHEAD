#!/usr/bin/env python3
"""
onshape_build_exploded.py
=========================
Build an exploded-view model WITHOUT creating anything in Onshape.

Instead of inserting parts and transforming them one API call at a time
(O(N) writes), this makes a fixed handful of READ calls and bakes the explode
locally:

  1. GET exploded views            -> pick the view
  2. GET assembly glTF             -> assembled geometry (one node per instance)
  3. GET assembly definition       -> assembled transforms + instance names
  4. GET assembly + explodedViewId -> exploded transforms
  5. GET assembly BOM              -> parts list

Each glTF instance node is matched to its occurrence (by full transform, with
the instance name as a tiebreak for coincident parts), and the node's transform
is rewritten to the exploded position. Only node transforms in the GLB's JSON
chunk change; the binary geometry buffer is untouched. No assembly is created
in Onshape, so there is nothing to clean up and the call count is constant
regardless of part count.

Outputs:  exploded_view.glb  and  parts.json

Usage
-----
    python onshape_build_exploded.py <source-assembly-url>
        [--glb out.glb] [--parts parts.json] [--bom-indented] [--emit]

Reads credentials from the same .onshape file as the other scripts.
"""

import os
import re
import sys
import json
import math
import base64
import struct
import logging
import threading
import unicodedata
from urllib.parse import urlencode, urlparse, quote

import requests

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

API_VERSION = "v16"
JSON_CHUNK = 0x4E4F534A
BIN_CHUNK  = 0x004E4942

# Matching tolerances (metres / unitless). Generous enough to absorb float
# rounding, tight enough to separate distinct parts; coincident parts are
# split by name.
TOL_T = 2e-3
TOL_R = 2e-2


# ── Config / URL (shared scaffolding) ────────────────────────────────────────

def load_config() -> dict:
    for path in (
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".onshape"),
        os.path.expanduser("~/.onshape"),
    ):
        if os.path.isfile(path):
            log.info("Loading credentials from %s", path)
            cfg = {}
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, _, v = line.partition("=")
                        cfg[k.strip().lower()] = v.strip()
            return cfg
    sys.exit("ERROR: .onshape credentials file not found.")


_URL_RE = re.compile(
    r"/documents/(?P<did>[^/]+)/(?P<wvm>[wvm])/(?P<wvm_id>[^/]+)/e/(?P<eid>[^/?#]+)"
)

def parse_url(url):
    p = urlparse(url)
    m = _URL_RE.search(p.path)
    if not m:
        sys.exit(f"ERROR: cannot parse URL: {url}")
    return f"{p.scheme}://{p.netloc}", m.group("did"), m.group("wvm"), m.group("wvm_id"), m.group("eid")


cfg = load_config()
ACCESS = cfg.get("access_key") or os.environ.get("ONSHAPE_ACCESS_KEY", "")
SECRET = cfg.get("secret_key") or os.environ.get("ONSHAPE_SECRET_KEY", "")
if not ACCESS or not SECRET:
    sys.exit("ERROR: access_key / secret_key not set.")


def _headers(accept):
    creds = base64.b64encode(f"{ACCESS}:{SECRET}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Accept": accept}


def api_get_json(url, params=None):
    if params:
        url += "?" + urlencode(params, quote_via=quote)
    r = requests.get(url, headers=_headers("application/json;charset=UTF-8; qs=0.09"), timeout=120)
    if not r.ok:
        log.error("HTTP %s\n%s", r.status_code, r.text[:500]); r.raise_for_status()
    return r.json()


def api_get_bytes(url, accept, params=None):
    if params:
        url += "?" + urlencode(params, quote_via=quote)
    done = threading.Event()

    def _heartbeat():
        secs = 0
        while not done.wait(20):
            secs += 20
            log.info("  … still waiting (%ds elapsed) …", secs)

    t = threading.Thread(target=_heartbeat, daemon=True)
    t.start()
    try:
        r = requests.get(url, headers=_headers(accept), timeout=300, stream=True)
        if not r.ok:
            log.error("HTTP %s\n%s", r.status_code, r.text[:500]); r.raise_for_status()
        chunks, downloaded, next_log = [], 0, 1 << 22  # first progress at 4 MB
        for chunk in r.iter_content(chunk_size=1 << 17):  # 128 KB chunks
            if chunk:
                chunks.append(chunk); downloaded += len(chunk)
                if downloaded >= next_log:
                    log.info("  … %.1f MB received", downloaded / (1 << 20))
                    next_log += 1 << 22
        return b"".join(chunks)
    finally:
        done.set(); t.join(1)


# ── 4x4 matrix helpers (row-major flat-16) ───────────────────────────────────

IDENTITY = [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]

def mat_mul(a, b):
    c = [0.0] * 16
    for i in range(4):
        for j in range(4):
            s = 0.0
            for k in range(4):
                s += a[i*4+k] * b[k*4+j]
            c[i*4+j] = s
    return c

def transpose16(m):
    return [m[c*4+r] for r in range(4) for c in range(4)]

def quat_to_rows(q):
    x, y, z, w = q
    return (
        1-2*(y*y+z*z), 2*(x*y-w*z),   2*(x*z+w*y),
        2*(x*y+w*z),   1-2*(x*x+z*z), 2*(y*z-w*x),
        2*(x*z-w*y),   2*(y*z+w*x),   1-2*(x*x+y*y),
    )

def trs_to_mat(t, q, s):
    r00,r01,r02, r10,r11,r12, r20,r21,r22 = quat_to_rows(q)
    sx, sy, sz = s
    return [
        r00*sx, r01*sy, r02*sz, t[0],
        r10*sx, r11*sy, r12*sz, t[1],
        r20*sx, r21*sy, r22*sz, t[2],
        0,0,0,1,
    ]

def mat_inverse(m):
    """General 4x4 inverse via Gauss-Jordan with partial pivoting (row-major)."""
    a = [[m[r*4+c] for c in range(4)] for r in range(4)]
    inv = [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]
    for col in range(4):
        piv = max(range(col, 4), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            raise ValueError("singular matrix")
        if piv != col:
            a[col], a[piv] = a[piv], a[col]
            inv[col], inv[piv] = inv[piv], inv[col]
        d = a[col][col]
        a[col] = [v / d for v in a[col]]
        inv[col] = [v / d for v in inv[col]]
        for r in range(4):
            if r == col:
                continue
            f = a[r][col]
            if f != 0.0:
                a[r] = [av - f*cv for av, cv in zip(a[r], a[col])]
                inv[r] = [iv - f*cv for iv, cv in zip(inv[r], inv[col])]
    return [inv[r][c] for r in range(4) for c in range(4)]

def trans_of(m):
    return (m[3], m[7], m[11])

def trans_dist(a, b):
    return math.sqrt((a[3]-b[3])**2 + (a[7]-b[7])**2 + (a[11]-b[11])**2)

def rot_maxdiff(a, b):
    idx = (0,1,2, 4,5,6, 8,9,10)
    return max(abs(a[i]-b[i]) for i in idx)

def close_xform(a, b):
    return trans_dist(a, b) <= TOL_T and rot_maxdiff(a, b) <= TOL_R


# ── GLB read / write (only the JSON chunk is rewritten) ──────────────────────

def read_glb(content):
    if content[:4] != b"glTF":
        # plain JSON glTF
        return json.loads(content.decode("utf-8")), None
    ver, total = struct.unpack_from("<II", content, 4)
    off = 12
    gltf = None
    chunks = []
    while off + 8 <= len(content):
        clen, ctype = struct.unpack_from("<II", content, off)
        data = content[off+8: off+8+clen]
        if ctype == JSON_CHUNK:
            gltf = json.loads(data.decode("utf-8"))
        else:
            chunks.append((ctype, data))
        off += 8 + clen
    if gltf is None:
        sys.exit("ERROR: no JSON chunk in GLB.")
    return gltf, chunks

def write_glb(gltf, chunks):
    jb = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    if len(jb) % 4:
        jb += b" " * (4 - len(jb) % 4)
    body = bytearray()
    body += struct.pack("<II", len(jb), JSON_CHUNK) + jb
    for ctype, data in chunks:
        d = data
        if len(d) % 4:
            pad = b"\x00" if ctype == BIN_CHUNK else b" "
            d = d + pad * (4 - len(d) % 4)
        body += struct.pack("<II", len(d), ctype) + d
    return b"glTF" + struct.pack("<II", 2, 12 + len(body)) + bytes(body)


# ── glTF scene analysis ──────────────────────────────────────────────────────

def analyse_gltf(gltf):
    nodes  = gltf.get("nodes", [])
    scenes = gltf.get("scenes", [])
    scene  = gltf.get("scene", 0)
    root_ids = set(scenes[scene]["nodes"]) if scenes else set(range(len(nodes)))

    parent = [None] * len(nodes)
    for i, n in enumerate(nodes):
        for c in n.get("children", []):
            parent[c] = i

    def local(n):
        if "matrix" in n:
            return transpose16([float(x) for x in n["matrix"]])  # col-major -> row-major
        t = n.get("translation", [0, 0, 0])
        q = n.get("rotation", [0, 0, 0, 1])
        s = n.get("scale", [1, 1, 1])
        return trs_to_mat(t, q, s)

    loc = [local(n) for n in nodes]
    world = [None] * len(nodes)

    def get_world(i):
        if world[i] is not None:
            return world[i]
        p = parent[i]
        world[i] = loc[i] if p is None else mat_mul(get_world(p), loc[i])
        return world[i]

    for i in range(len(nodes)):
        get_world(i)

    depth = []
    for i in range(len(nodes)):
        d, p = 0, parent[i]
        while p is not None:
            d += 1; p = parent[p]
        depth.append(d)

    has_mesh = [False] * len(nodes)
    def calc(i):
        m = "mesh" in nodes[i]
        for c in nodes[i].get("children", []):
            if calc(c):
                m = True
        has_mesh[i] = m
        return m
    for r in root_ids:
        calc(r)

    names = [n.get("name", "") for n in nodes]
    return nodes, parent, world, depth, has_mesh, names, root_ids


# ── Occurrence helpers ───────────────────────────────────────────────────────

def occ_map(asm_def):
    out = {}
    for o in asm_def.get("rootAssembly", {}).get("occurrences", []):
        t = o.get("transform")
        if t and len(t) == 16:
            out[tuple(o.get("path", []))] = [float(x) for x in t]
    return out

def name_map(asm_def):
    idn = {}
    for inst in asm_def.get("rootAssembly", {}).get("instances", []):
        if inst.get("id"):
            idn[inst["id"]] = inst.get("name", "")
    for sub in asm_def.get("subAssemblies", []):
        for inst in sub.get("instances", []):
            if inst.get("id"):
                idn.setdefault(inst["id"], inst.get("name", ""))
    return idn

def leaf_paths(paths):
    pathset = list(paths)
    leaves = []
    for p in pathset:
        if not any(len(q) > len(p) and q[:len(p)] == p for q in pathset):
            leaves.append(p)
    return leaves


def slugify(name, fallback="exploded_view"):
    """Filesystem-safe base name derived from the assembly name."""
    s = unicodedata.normalize("NFKD", name or "")
    s = re.sub(r"[^\w\- ]+", "", s).strip()
    s = re.sub(r"[\s]+", "_", s)
    return s or fallback


def fetch_assembly_name(base, did, wvm, wvm_id, eid):
    """Best-effort: the element's tab name. Returns '' on failure."""
    url = f"{base}/api/{API_VERSION}/documents/d/{did}/{wvm}/{wvm_id}/elements"
    try:
        els = api_get_json(url, {"elementId": eid})
        for el in (els if isinstance(els, list) else []):
            if el.get("id") == eid:
                return el.get("name", "")
        if els:
            return els[0].get("name", "")
    except Exception as e:
        log.info("Could not fetch assembly name (%s); using default filename.", e)
    return ""


# ── BOM (mirror of onshape_export_bom.py) ────────────────────────────────────

def norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).lower()
    s = re.sub(r"<[^>]*>", " ", s)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def _find_header(headers, *needles):
    for h in headers:
        label = (h.get("name") or "").lower()
        prop = (h.get("propertyName") or "").lower()
        for n in needles:
            if n in label or n in prop:
                return h.get("id")
    return None

def fetch_bom(base, did, wvm, wvm_id, eid, indented):
    params = {"indented": "true" if indented else "false",
              "generateIfAbsent": "true", "onlyVisibleColumns": "false"}
    url = f"{base}/api/{API_VERSION}/assemblies/d/{did}/{wvm}/{wvm_id}/e/{eid}/bom"
    data = api_get_json(url, params)
    table = data.get("bomTable", data)
    headers = table.get("headers", []) or data.get("headers", [])
    rows = table.get("rows", []) or data.get("rows", []) or data.get("bomTableRows", [])
    if not headers or not rows:
        log.warning("BOM had no headers/rows; parts.json will be empty.")
        return {"version": 1, "items": []}
    h_item = _find_header(headers, "item")
    h_name = _find_header(headers, "name", "description")
    h_pn   = _find_header(headers, "part number", "partnumber")
    h_qty  = _find_header(headers, "quantity", "qty")

    def cell(row, hid):
        if not hid:
            return ""
        vals = row.get("headerIdToValue", row.get("headerIdToValues", {})) or {}
        v = vals.get(hid, "")
        if isinstance(v, dict):
            v = v.get("value") or v.get("displayName") or ""
        return "" if v is None else str(v)

    items = []
    for i, row in enumerate(rows, 1):
        name = cell(row, h_name); pn = cell(row, h_pn)
        items.append({
            "item": cell(row, h_item) or str(i),
            "name": name, "partNumber": pn,
            "quantity": cell(row, h_qty),
            "match": [m for m in {norm(name), norm(pn)} if m],
        })
    return {"version": 1, "items": items}


# ── Diagnostics ──────────────────────────────────────────────────────────────

def run_diagnose(gltf, assembled, exploded, view=None):
    print("\n" + "=" * 72)
    print("DIAGNOSE — why some glTF parts have no exploded transform")
    print("=" * 72)

    rawE = exploded.get("rootAssembly", {}).get("occurrences", [])
    rawA = assembled.get("rootAssembly", {}).get("occurrences", [])

    def hist(occ):
        h = {}
        for o in occ:
            n = len(o.get("path", []))
            h[n] = h.get(n, 0) + 1
        return dict(sorted(h.items()))

    def bad(occ):
        return [o for o in occ if not (o.get("transform") and len(o["transform"]) == 16)]

    print(f"\nexploded occurrences : total={len(rawE)}  "
          f"path-length-hist={hist(rawE)}  bad-transform={len(bad(rawE))}")
    print(f"assembled occurrences: total={len(rawA)}  "
          f"path-length-hist={hist(rawA)}  bad-transform={len(bad(rawA))}")

    E = occ_map(exploded)
    A = occ_map(assembled)
    leaves_E = leaf_paths(list(E.keys()))
    leaves_rawE = leaf_paths([tuple(o.get("path", [])) for o in rawE])
    print(f"\nleaves from E (valid-transform map): {len(leaves_E)}")
    print(f"leaves from raw exploded paths     : {len(leaves_rawE)}")
    if len(leaves_rawE) != len(leaves_E):
        print("  -> occ_map dropped occurrences with missing/short transforms "
              "(likely the culprit).")

    names = name_map(assembled) or name_map(exploded)
    root_instances = {i.get("name", "") for i in
                      assembled.get("rootAssembly", {}).get("instances", [])}
    sub_instances = {}
    for si, sub in enumerate(assembled.get("subAssemblies", [])):
        for inst in sub.get("instances", []):
            sub_instances.setdefault(inst.get("name", ""), []).append(si)
    print(f"\nsubAssemblies present: {len(assembled.get('subAssemblies', []))}  "
          f"(root instances={len(root_instances)}, "
          f"distinct sub-assembly instance names={len(sub_instances)})")

    nodes, parent, world, depth, has_mesh, nn, roots = analyse_gltf(gltf)
    inst_nodes = [i for i in range(len(nodes))
                  if has_mesh[i] and i not in roots and depth[i] == 1]
    print(f"glTF depth-1 instance nodes: {len(inst_nodes)}")

    # glTF geometry sits at ASSEMBLED positions, so match against A (assembled),
    # exactly as the real matcher does.
    leaves_A = leaf_paths(list(A.keys()))
    Alist = [(p, A[p]) for p in leaves_A]
    all_paths = set(A.keys()) | set(E.keys())

    print("\n" + "-" * 72)
    print("UNMATCHED glTF NODES (no assembled occurrence within tolerance)")
    print("-" * 72)
    any_unmatched = False
    unmatched_names = []
    for i in inst_nodes:
        best, bd = None, 1e9
        for p, t in Alist:
            d = trans_dist(world[i], t)
            if d < bd:
                bd, best = d, p
        matched = best is not None and bd <= TOL_T and rot_maxdiff(world[i], A[best]) <= TOL_R
        if matched:
            continue
        any_unmatched = True
        nm = nn[i]
        unmatched_names.append(nm)
        if nm in root_instances:
            origin = "ROOT instance"
        elif nm in sub_instances:
            origin = f"SUB-ASSEMBLY instance (subAssemblies index {sub_instances[nm]})"
        else:
            origin = "NOT FOUND in any instances[] list"
        same_name = [list(p) for p in all_paths if names.get(p[-1], "") == nm]
        in_e = [list(p) for p in E.keys() if names.get(p[-1], "") == nm]
        print(f"\n[{i}] {nm!r}")
        print(f"     world T=({world[i][3]:.4f}, {world[i][7]:.4f}, {world[i][11]:.4f})")
        print(f"     nearest ASSEMBLED occ: dist={bd:.4f} m  path={list(best) if best else None}"
              f"  name={names.get(best[-1], '') if best else ''!r}")
        print(f"     name origin: {origin}")
        print(f"     occurrences (any state) with this name: {same_name or 'NONE'}")
        print(f"     exploded occurrences with this name:    {in_e or 'NONE'}")

    if not any_unmatched:
        print("\n(no unmatched nodes this run)")

    # Full instance metadata for the unmatched parts — reveals how composite /
    # imported / standard-content parts differ from ordinary ones.
    if unmatched_names:
        print("\n" + "-" * 72)
        print("INSTANCE RECORDS FOR UNMATCHED PARTS")
        print("-" * 72)
        records = {}  # name -> list of (origin, record)
        for inst in assembled.get("rootAssembly", {}).get("instances", []):
            records.setdefault(inst.get("name", ""), []).append(("root", inst))
        for si, sub in enumerate(assembled.get("subAssemblies", [])):
            for inst in sub.get("instances", []):
                records.setdefault(inst.get("name", ""), []).append((f"sub[{si}]", inst))
        for nm in unmatched_names:
            recs = records.get(nm)
            print(f"\n{nm!r}:")
            if not recs:
                print("   no instance record found under this name.")
                continue
            for origin, r in recs:
                keys = ("id", "type", "partId", "elementId", "documentId", "documentVersion",
                        "documentMicroversion", "configuration", "isStandardContent",
                        "suppressed", "partNumber")
                slim = {k: r[k] for k in keys if k in r}
                print(f"   [{origin}] {json.dumps(slim)}")

    # Exploded-view definition — may carry per-part explode displacements even
    # for parts that are absent from the occurrence list.
    if view is not None:
        print("\n" + "-" * 72)
        print("EXPLODED VIEW DEFINITION")
        print("-" * 72)
        print("top-level keys:", ", ".join(sorted(view.keys())))
        blob = json.dumps(view, indent=2)
        if len(blob) > 2500:
            blob = blob[:2500] + "\n… (truncated)"
        print(blob)

    print("\n" + "=" * 72)
    print("READING THE RESULT")
    print("=" * 72)
    print("""\
  • 'leaves from raw' > 'leaves from E'  -> transforms were missing/short; the
        occurrence exists but had no usable matrix (fix: read it differently).
  • name origin = SUB-ASSEMBLY  AND  occurrences-with-name = NONE  -> the part
        is nested and its leaf occurrence isn't being enumerated (fix: recurse
        sub-assemblies / use a path the explode call returns).
  • unmatched parts share a 'type' / flag (e.g. composite) absent on matched
        ones  -> they are enumerated differently; we read their explode position
        from the exploded-view definition above or a dedicated query.
  • nearest assembled occ dist is large -> no occurrence near the part at all.
  • nearest dist small but still unmatched -> rotation tolerance issue.
""")


# ── Probe: does the glTF endpoint honour explodedViewId? ─────────────────────

def probe_gltf_explode(asm, view_id):
    print("\n" + "=" * 72)
    print("PROBE — does GET /gltf?explodedViewId= return EXPLODED geometry?")
    print("=" * 72)

    def name_to_translations(g):
        nodes, parent, world, depth, has_mesh, nn, roots = analyse_gltf(g)
        out = {}
        for i in range(len(nodes)):
            if has_mesh[i] and i not in roots and depth[i] == 1:
                out.setdefault(nn[i], []).append(trans_of(world[i]))
        return out

    log.info("Fetching ASSEMBLED glTF …")
    a, _ = read_glb(api_get_bytes(asm + "/gltf", "model/gltf-binary"))
    log.info("Fetching glTF WITH explodedViewId …")
    try:
        b, _ = read_glb(api_get_bytes(asm + "/gltf", "model/gltf-binary",
                                      {"explodedViewId": view_id}))
    except Exception as e:
        print(f"\nThe exploded glTF request FAILED: {e}")
        print("=> endpoint does not accept explodedViewId; we'll use another route.")
        return

    ta, tb = name_to_translations(a), name_to_translations(b)
    total = moved = 0
    maxd = 0.0
    movers = []
    for nm, la in ta.items():
        lb = tb.get(nm)
        if not lb:
            continue
        for pa, pb in zip(sorted(la), sorted(lb)):
            total += 1
            d = math.dist(pa, pb)
            maxd = max(maxd, d)
            if d > 1e-4:
                moved += 1
                if len(movers) < 8:
                    movers.append((nm, round(d, 4)))
    print(f"\nnodes compared : {total}")
    print(f"nodes that moved: {moved}")
    print(f"max displacement: {maxd:.4f} m")
    if movers:
        print("examples:")
        for nm, d in movers:
            print(f"    {d:>7} m   {nm}")
    print()
    if moved > 0:
        print("=> EXPLODED glTF IS honoured. Best fix: export the exploded glTF")
        print("   directly and skip occurrence-matching entirely — composites included.")
    else:
        print("=> exploded glTF is identical to assembled (not honoured). We'll need")
        print("   the exploded-view feature definition for the composite displacements.")


# ── Probe: where does the exploded-view STEP/displacement data live? ─────────

def probe_explode_data(asm):
    print("\n" + "=" * 72)
    print("PROBE — locate exploded-view step / displacement data")
    print("=" * 72)

    def trunc(s, n=3000):
        return s if len(s) <= n else s[:n] + "\n… (truncated)"

    composites = ["Lock Nut", "Angular Contact Bearing",
                  "Cylindrical Roller Bearing", "Radial Seal"]

    # 1) raw exploded-views response in full
    print("\n--- RAW /explodedviews ---")
    try:
        raw = api_get_json(asm + "/explodedviews")
    except Exception as e:
        raw = None
        print("request failed:", e)
    if raw is not None:
        print(trunc(json.dumps(raw, indent=2)))

    # 2) assembly features (mates, groups, possibly explode features)
    print("\n--- /features (assembly feature list) ---")
    feats = None
    try:
        feats = api_get_json(asm + "/features")
    except Exception as e:
        print("request failed:", e)
    if feats is not None:
        flist = feats.get("features", feats if isinstance(feats, list) else [])
        print(f"feature count: {len(flist)}")
        explode_feats = []
        for f in flist:
            msg = f.get("message", {})
            ft = msg.get("featureType", f.get("typeName", "?"))
            fn = msg.get("name", "?")
            print(f"   type={ft!r}  name={fn!r}")
            if "explod" in str(ft).lower() or "explod" in str(fn).lower():
                explode_feats.append(f)
        for f in explode_feats[:2]:
            print("\n   --- explode-related feature (full) ---")
            print(trunc(json.dumps(f, indent=2)))

    # 3) do these sources reference the composite parts at all?
    blob_v = json.dumps(raw) if raw is not None else ""
    blob_f = json.dumps(feats) if feats is not None else ""
    print("\n--- composite references ---")
    for nm in composites:
        print(f"   {nm!r}: in explodedviews={nm in blob_v}  in features={nm in blob_f}")

    print("\n" + "=" * 72)
    print("READING THE RESULT")
    print("=" * 72)
    print("""\
  • If /explodedviews or a feature contains per-step displacements that
        reference the composite instances -> we can compute their exploded
        positions (assembled-from-glTF + displacement) and place them. CODE FIX.
  • If neither source exposes the explode steps or the composites -> Onshape's
        REST API does not surface composite-part explode data, and the practical
        options are a manual nudge in the annotator or dissolving the composites
        in Onshape so they enumerate normally. WORKFLOW FIX.
""")


# ── Probe: occurrence list vs glTF nodes (composite mapping) ─────────────────

def probe_occ_vs_gltf(gltf, assembled, exploded):
    print("\n" + "=" * 72)
    print("PROBE — occurrences vs glTF nodes (how composites map)")
    print("=" * 72)

    inst_by_id = {}
    for inst in assembled.get("rootAssembly", {}).get("instances", []):
        if inst.get("id"):
            inst_by_id[inst["id"]] = inst
    for sub in assembled.get("subAssemblies", []):
        for inst in sub.get("instances", []):
            if inst.get("id"):
                inst_by_id.setdefault(inst["id"], inst)

    A = occ_map(assembled)
    E = occ_map(exploded)
    leaves = leaf_paths(list(A.keys()))

    nodes, parent, world, depth, has_mesh, nn, roots = analyse_gltf(gltf)
    inst_nodes = [i for i in range(len(nodes))
                  if has_mesh[i] and i not in roots and depth[i] == 1]

    # Replicate the real matcher: occurrence -> node by transform (+name tiebreak)
    names = name_map(assembled)
    used = set()
    occ_to_node = {}
    for path in leaves:
        a = A.get(path)
        nm = names.get(path[-1], "") if path else ""
        cand = [i for i in inst_nodes
                if i not in used and a is not None and close_xform(world[i], a)]
        if not cand:
            occ_to_node[path] = None
            continue
        cand.sort(key=lambda i: (0 if nn[i] == nm and nm else 1, depth[i],
                                  trans_dist(world[i], a)))
        occ_to_node[path] = cand[0]
        used.add(cand[0])

    node_to_occ = {v: k for k, v in occ_to_node.items() if v is not None}

    print(f"\nleaf occurrences: {len(leaves)}   glTF depth-1 mesh nodes: {len(inst_nodes)}")

    print("\n--- OCCURRENCES (leaf) ---")
    for path in sorted(leaves, key=lambda p: (round(A[p][7], 3), round(A[p][3], 3))):
        a = A[path]
        inst = inst_by_id.get(path[-1], {})
        ni = occ_to_node.get(path)
        tag = f"node[{ni}] {nn[ni]!r}" if ni is not None else "—— no node ——"
        print(f"  T=({a[3]:+.4f},{a[7]:+.4f},{a[11]:+.4f})  {tag}")
        print(f"        occ name={inst.get('name','?')!r} type={inst.get('type','?')!r} "
              f"partId={inst.get('partId','?')!r} elem={inst.get('elementId','?')!r}")

    print("\n--- glTF NODES (depth-1, has mesh) ---")
    for i in sorted(inst_nodes, key=lambda i: (round(world[i][7], 3), round(world[i][3], 3))):
        w = world[i]
        if i in node_to_occ:
            mark = "matched"
        else:
            mark = "UNMATCHED"
        print(f"  T=({w[3]:+.4f},{w[7]:+.4f},{w[11]:+.4f})  [{mark}]  {nn[i]!r}")

    unmatched = [i for i in inst_nodes if i not in node_to_occ]
    print(f"\n--- {len(unmatched)} UNMATCHED NODE(S): nearest matched occurrence ---")
    for i in unmatched:
        best, bd = None, 1e9
        for path, ni in occ_to_node.items():
            if ni is None:
                continue
            d = trans_dist(world[i], A[path])
            if d < bd:
                bd, best = d, path
        inst = inst_by_id.get(best[-1], {}) if best else {}
        print(f"  node {nn[i]!r}  ->  nearest occ {inst.get('name','?')!r} "
              f"(partId={inst.get('partId','?')!r}) at dist={bd:.4f} m")

    print("\n" + "=" * 72)
    print("READING THE RESULT")
    print("=" * 72)
    print("""\
  • If several UNMATCHED nodes cluster around one matched occurrence whose
        partId differs from ordinary parts -> that occurrence is a composite and
        the unmatched nodes are its constituent bodies. Fix: move every body of
        a composite by that occurrence's delta (E·A⁻¹), as a group.
  • If matched composite occurrences share an elementId with the unmatched
        nodes' source -> we can group by source element.
""")


# ── Probe: does the glTF group composite bodies under a shared parent? ───────

def probe_gltf_grouping(asm):
    print("\n" + "=" * 72)
    print("PROBE — does the glTF group composite bodies (clean-fix viability)")
    print("=" * 72)

    log.info("Fetching assembly glTF …")
    gltf, _ = read_glb(api_get_bytes(asm + "/gltf", "model/gltf-binary"))
    log.info("Fetching assembly definition (assembled) …")
    assembled = api_get_json(asm, {"includeMateFeatures": "false",
                                   "includeNonSolids": "true",
                                   "includeMateConnectors": "false"})

    A = occ_map(assembled)
    leaves = leaf_paths(list(A.keys()))
    names = name_map(assembled)
    nodes, parent, world, depth, has_mesh, nn, roots = analyse_gltf(gltf)
    inst_nodes = [i for i in range(len(nodes))
                  if has_mesh[i] and i not in roots and depth[i] == 1]

    # replicate matcher to find orphans
    used = set()
    for path in leaves:
        a = A.get(path)
        nm = names.get(path[-1], "") if path else ""
        cand = [i for i in inst_nodes
                if i not in used and a is not None and close_xform(world[i], a)]
        if not cand:
            continue
        cand.sort(key=lambda i: (0 if nn[i] == nm and nm else 1, depth[i],
                                  trans_dist(world[i], a)))
        used.add(cand[0])
    orphans = [i for i in inst_nodes if i not in used]

    def mesh_children(i):
        return [c for c in nodes[i].get("children", []) if has_mesh[c]]

    print("\n--- nodes grouping >1 mesh-bearing child (would-be composite wrappers) ---")
    found_group = False
    for i in range(len(nodes)):
        mc = mesh_children(i)
        if len(mc) > 1 and i not in roots:
            found_group = True
            print(f"  [{i}] {nn[i]!r} (depth {depth[i]}) -> {len(mc)} mesh children: "
                  f"{[nn[c] for c in mc][:6]}")
    if not found_group:
        print("  NONE found below the scene root.")

    print(f"\n--- ancestry of the {len(orphans)} orphan node(s) ---")
    for i in orphans:
        chain, p = [], i
        while p is not None:
            chain.append(f"[{p}]{nn[p]!r}")
            p = parent[p]
        print(f"\n  orphan [{i}] {nn[i]!r}  depth={depth[i]}")
        print("    ancestry leaf->root: " + " <- ".join(chain))
        par = parent[i]
        if par is not None:
            sibs = nodes[par].get("children", [])
            print(f"    parent [{par}] {nn[par]!r} has {len(sibs)} child(ren):")
            for c in sibs:
                tag = "matched" if c in used else ("ORPHAN" if c in orphans else "—")
                if has_mesh[c]:
                    print(f"        [{c}] {nn[c]!r}  ({tag})")

    print("\n" + "=" * 72)
    print("READING THE RESULT")
    print("=" * 72)
    print("""\
  • A would-be composite wrapper exists (a node with several mesh children),
        OR an orphan shares a non-root parent with its matched sibling
        -> the glTF DOES carry the grouping. CLEAN FIX: match and move at the
        wrapper level; stays at 5 calls, no new data.
  • No grouping node, and every orphan's parent is the scene root (same parent
        as everything else) -> the glTF is fully flat; the 5-call clean fix is
        not possible and we use the bounded per-element fallback instead.
""")


# ── Main ─────────────────────────────────────────────────────────────────────






def main():
    flags = sys.argv[1:]
    args = [a for a in flags if not a.startswith("--")]
    if not args:
        sys.exit("Usage: python onshape_build_exploded.py <source-assembly-url> "
                 "[--glb out.glb] [--parts parts.json] [--bom-indented] [--emit]")

    def flag(name, default=None):
        for i, a in enumerate(flags):
            if a == name and i + 1 < len(flags):
                return flags[i + 1]
            if a.startswith(name + "="):
                return a.split("=", 1)[1]
        return default

    glb_out   = flag("--glb")
    parts_out = flag("--parts")
    outdir    = flag("--outdir", ".")
    indented  = "--bom-indented" in flags
    emit      = "--emit" in flags

    base, did, wvm, wvm_id, eid = parse_url(args[0].strip())
    asm = f"{base}/api/{API_VERSION}/assemblies/d/{did}/{wvm}/{wvm_id}/e/{eid}"

    if "--probe-explode-data" in flags:
        probe_explode_data(asm)
        return

    if "--probe-gltf-grouping" in flags:
        probe_gltf_grouping(asm)
        return

    # 1) exploded view id
    log.info("Listing exploded views …")
    views = api_get_json(asm + "/explodedviews")
    views = views if isinstance(views, list) else views.get("explodedViews", [])
    if not views:
        sys.exit("ERROR: this assembly has no exploded views.")
    want = cfg.get("exploded_view_id") or os.environ.get("ONSHAPE_EXPLODED_VIEW_ID", "")
    view = next((v for v in views if v["id"] == want), None) if want else views[0]
    if view is None:
        sys.exit(f"ERROR: exploded view {want!r} not found. Available: {[v['id'] for v in views]}")
    log.info("Using exploded view %r (id=%s)", view.get("name", "?"), view["id"])

    if "--probe-gltf-explode" in flags:
        probe_gltf_explode(asm, view["id"])
        return

    # 2) glTF (assembled geometry)
    log.info("Fetching assembly glTF (Onshape generates this server-side — may take 30–120 s) …")
    content = api_get_bytes(asm + "/gltf", "model/gltf-binary")
    gltf, chunks = read_glb(content)
    is_glb = chunks is not None
    log.info("glTF parsed (%s).", "GLB" if is_glb else "JSON")

    # 3) assembled definition (transforms + names)
    log.info("Fetching assembly definition (assembled) …")
    # includeNonSolids=true: composite parts appear to be classified as
    # non-solid by this filter; with false they vanish from occurrences and
    # instances entirely (6 composite instances missing on the test assembly).
    asm_params = {"includeMateFeatures": "false", "includeNonSolids": "true",
                  "includeMateConnectors": "false"}
    assembled = api_get_json(asm, asm_params)

    asm_name = fetch_assembly_name(base, did, wvm, wvm_id, eid)
    slug = slugify(asm_name)
    if not glb_out:
        glb_out = os.path.join(outdir, slug + ".glb")
    if not parts_out:
        parts_out = os.path.join(outdir, slug + ".parts.json")
    log.info("Assembly name: %r  ->  outputs %s / %s",
             asm_name or "(unknown)", os.path.basename(glb_out), os.path.basename(parts_out))

    # 4) exploded definition (exploded transforms)
    log.info("Fetching assembly definition (exploded) …")
    exploded = api_get_json(asm, dict(asm_params, explodedViewId=view["id"]))

    if "--diagnose" in flags:
        run_diagnose(gltf, assembled, exploded, view)
        return

    if "--probe-occ-vs-gltf" in flags:
        probe_occ_vs_gltf(gltf, assembled, exploded)
        return

    A = occ_map(assembled)
    E = occ_map(exploded)
    names = name_map(assembled) or name_map(exploded)

    leaves = leaf_paths(list(E.keys()))
    log.info("Exploded view has %d leaf occurrence(s).", len(leaves))

    # ── Match glTF instance nodes to occurrences and bake ─────────────────────
    nodes, parent, world, depth, has_mesh, node_names, root_ids = analyse_gltf(gltf)

    used = set()
    bakes = []          # (node_index, E_transform)
    unmatched_occ = []
    for path in leaves:
        e = E[path]
        a = A.get(path)
        nm = names.get(path[-1], "") if path else ""
        cand = []
        for i in range(len(nodes)):
            if i in root_ids or not has_mesh[i] or i in used:
                continue
            if a is not None:
                if not close_xform(world[i], a):
                    continue
            else:
                if node_names[i] != nm or not nm:
                    continue
            cand.append(i)
        if not cand:
            unmatched_occ.append((path, nm))
            continue
        cand.sort(key=lambda i: (0 if node_names[i] == nm and nm else 1,
                                 depth[i],
                                 trans_dist(world[i], a) if a else 0.0))
        pick = cand[0]
        used.add(pick)
        bakes.append((pick, e))

    # Report
    matched = len(bakes)
    log.info("Matched %d / %d occurrence(s) to glTF nodes.", matched, len(leaves))
    if unmatched_occ:
        log.warning("%d occurrence(s) had no matching glTF node "
                    "(they will stay at their assembled position):", len(unmatched_occ))
        for path, nm in unmatched_occ[:12]:
            log.warning("    %r  path=%s", nm or "?", list(path))
    leftover = [i for i in range(len(nodes))
                if has_mesh[i] and i not in root_ids and i not in used and depth[i] == 1]
    if leftover:
        log.warning("%d glTF instance node(s) were not assigned an exploded "
                    "transform:", len(leftover))
        for i in leftover[:12]:
            log.warning("    [%d] %r", i, node_names[i])

    # Apply: rewrite each matched node's local transform so its WORLD becomes E.
    for i, e in bakes:
        p = parent[i]
        parent_world = world[p] if p is not None else IDENTITY
        new_local = mat_mul(mat_inverse(parent_world), e)
        node = nodes[i]
        node.pop("translation", None)
        node.pop("rotation", None)
        node.pop("scale", None)
        node["matrix"] = [round(v, 9) for v in transpose16(new_local)]  # row-major -> col-major

    # Write the exploded GLB (binary geometry untouched).
    if is_glb:
        out_bytes = write_glb(gltf, chunks)
        if not glb_out.lower().endswith(".glb"):
            glb_out = os.path.splitext(glb_out)[0] + ".glb"
        with open(glb_out, "wb") as f:
            f.write(out_bytes)
    else:
        if not glb_out.lower().endswith(".gltf"):
            glb_out = os.path.splitext(glb_out)[0] + ".gltf"
        with open(glb_out, "w") as f:
            json.dump(gltf, f)
    log.info("Wrote %s", glb_out)

    # 5) BOM
    log.info("Fetching BOM …")
    bom = fetch_bom(base, did, wvm, wvm_id, eid, indented)
    with open(parts_out, "w") as f:
        json.dump(bom, f, indent=2)
    log.info("Wrote %s (%d BOM items)", parts_out, len(bom["items"]))

    if emit:
        print("EVRESULT " + json.dumps({
            "glb": glb_out, "parts": parts_out,
            "assembly": asm_name, "slug": slug,
            "matched": matched, "leaves": len(leaves),
            "unmatched_occ": len(unmatched_occ), "leftover_nodes": len(leftover),
        }), flush=True)

    if unmatched_occ or leftover:
        log.warning("Completed with matching gaps — see warnings above.")


if __name__ == "__main__":
    main()
