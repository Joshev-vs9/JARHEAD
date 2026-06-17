#!/usr/bin/env python3
"""
onshape_export_bom.py
=====================
Exports the Bill of Materials from an Onshape assembly as a parts-list sidecar
(parts.json) for the exploded-view annotator. The annotator uses it to:
  * number balloons to match the Onshape BOM item numbers, and
  * label each balloon with the part name and part number.

IMPORTANT: point this at the ORIGINAL/source assembly — the one whose BOM you
want the balloon numbers to match — NOT the generated exploded-layout assembly.

Usage
-----
    python onshape_export_bom.py <source-assembly-url> [parts.json]

    --indented        Pull an indented (multi-level) BOM so parts nested in
                      sub-assemblies appear as their own rows. Item numbers
                      then look like 1, 1.1, 2 … . Default is a flat top-level
                      BOM with simple integer item numbers.
    --dump            Print the raw BOM JSON and exit (use this to inspect
                      column headers if names/numbers come out blank).

Config file  (.onshape)
-----------------------
Same file as the other scripts; only access_key / secret_key are used here.
"""

import os
import re
import sys
import json
import base64
import logging
import unicodedata
from typing import Optional, Tuple
from urllib.parse import urlencode, urlparse, quote

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

API_VERSION = "v16"


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
    return {}


_URL_RE = re.compile(
    r"/documents/(?P<did>[^/]+)/(?P<wvm>[wvm])/(?P<wvm_id>[^/]+)/e/(?P<eid>[^/?#]+)"
)

def parse_onshape_url(url: str) -> Tuple[str, str, str, str, str]:
    parsed = urlparse(url)
    base   = f"{parsed.scheme}://{parsed.netloc}"
    m      = _URL_RE.search(parsed.path)
    if not m:
        sys.exit(f"ERROR: Could not parse the Onshape URL:\n  {url}")
    return base, m.group("did"), m.group("wvm"), m.group("wvm_id"), m.group("eid")


# ── Matching normaliser (mirror of the annotator's norm()) ───────────────────

def norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).lower()
    s = re.sub(r"<[^>]*>", " ", s)     # drop instance suffix  <2>
    s = re.sub(r"\([^)]*\)", " ", s)   # drop parenthetical    (2)
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# ── Header resolution ────────────────────────────────────────────────────────

def _find_header(headers, *needles) -> Optional[str]:
    """Return the header id whose name/propertyName matches any needle."""
    for h in headers:
        label = (h.get("name") or "").lower()
        prop  = (h.get("propertyName") or "").lower()
        for n in needles:
            if n in label or n in prop:
                return h.get("id")
    return None


def main() -> None:
    cfg = load_config()
    ak = cfg.get("access_key") or os.environ.get("ONSHAPE_ACCESS_KEY", "")
    sk = cfg.get("secret_key") or os.environ.get("ONSHAPE_SECRET_KEY", "")
    if not ak or not sk:
        sys.exit("ERROR: Credentials not found (see .onshape file).")

    flags = sys.argv[1:]
    args  = [a for a in flags if not a.startswith("--")]
    if not args:
        sys.exit("Usage: python onshape_export_bom.py <source-assembly-url> [parts.json]")

    indented = "--indented" in flags
    dump     = "--dump" in flags
    base_url, did, wvm, wvm_id, eid = parse_onshape_url(args[0].strip())
    out_path = args[1] if len(args) > 1 else "parts.json"

    creds  = base64.b64encode(f"{ak}:{sk}".encode()).decode()
    params = {
        "indented":         "true" if indented else "false",
        "generateIfAbsent": "true",
        "onlyVisibleColumns": "false",
    }
    url = (f"{base_url}/api/{API_VERSION}/assemblies/d/{did}/{wvm}/{wvm_id}/e/{eid}"
           f"/bom?{urlencode(params, quote_via=quote)}")

    log.info("Fetching BOM from: %s", url)
    r = requests.get(
        url,
        headers={"Authorization": f"Basic {creds}",
                 "Accept": "application/json;charset=UTF-8; qs=0.09"},
        timeout=120,
    )
    if not r.ok:
        log.error("HTTP %s\n%s", r.status_code, r.text[:600])
        r.raise_for_status()
    data = r.json()

    if dump:
        print(json.dumps(data, indent=2))
        return

    # BOM table location varies slightly across deployments.
    table   = data.get("bomTable", data)
    headers = table.get("headers", []) or data.get("headers", [])
    rows    = table.get("rows", []) or data.get("rows", []) or data.get("bomTableRows", [])

    if not headers or not rows:
        sys.exit("ERROR: BOM response had no headers/rows. Re-run with --dump "
                 "to inspect the structure and tell me what it looks like.")

    h_item = _find_header(headers, "item")
    h_name = _find_header(headers, "name", "description")
    h_pn   = _find_header(headers, "part number", "partnumber")
    h_qty  = _find_header(headers, "quantity", "qty")

    log.info("Header map  item=%s  name=%s  partNumber=%s  qty=%s",
             h_item, h_name, h_pn, h_qty)

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
        item = cell(row, h_item) or str(i)
        name = cell(row, h_name)
        pn   = cell(row, h_pn)
        qty  = cell(row, h_qty)
        match = [m for m in {norm(name), norm(pn)} if m]
        items.append({
            "item":       item,
            "name":       name,
            "partNumber": pn,
            "quantity":   qty,
            "match":      match,
        })

    out = {"version": 1, "source": {"documentId": did, "elementId": eid},
           "items": items}
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)

    log.info("Wrote %s  (%d BOM items)", out_path, len(items))
    blank = sum(1 for it in items if not it["name"] and not it["partNumber"])
    if blank:
        log.warning("%d item(s) have no name AND no part number — column "
                    "headers may not have matched. Re-run with --dump to check.",
                    blank)
    for it in items[:6]:
        log.info("  [%s] %s  (%s)", it["item"], it["name"] or "?", it["partNumber"] or "—")
    if len(items) > 6:
        log.info("  … %d more", len(items) - 6)


if __name__ == "__main__":
    main()
