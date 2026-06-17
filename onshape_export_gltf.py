#!/usr/bin/env python3
"""
onshape_export_gltf.py
======================
Exports an Onshape assembly as glTF (binary .glb preferred) for use in the
exploded-view annotator web app.

Point it at the EXPLODED layout assembly created by
onshape_explode_to_assembly.py (or any assembly).

Usage
-----
    python onshape_export_gltf.py <assembly-url> [output.glb]

    The URL is pasted straight from your browser while the assembly tab is
    open. Output defaults to exploded_view.glb in the current directory.

Optional tessellation quality flags:
    --angle-tolerance <deg>     e.g. 5   (smaller = smoother, bigger file)
    --chord-tolerance <m>      e.g. 0.0005

Config file  (.onshape)
-----------------------
Same file as the other scripts; only access_key / secret_key are used here.
"""

import os
import re
import sys
import base64
import logging
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


# ─────────────────────────────────────────────────────────────────────────────
# Config / URL (same scaffolding as the other scripts)
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".onshape"),
        os.path.expanduser("~/.onshape"),
    ]
    for path in candidates:
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
    r"/documents/(?P<did>[^/]+)"
    r"/(?P<wvm>[wvm])/(?P<wvm_id>[^/]+)"
    r"/e/(?P<eid>[^/?#]+)"
)

def parse_onshape_url(url: str) -> Tuple[str, str, str, str, str]:
    parsed = urlparse(url)
    base   = f"{parsed.scheme}://{parsed.netloc}"
    m      = _URL_RE.search(parsed.path)
    if not m:
        sys.exit(
            "ERROR: Could not parse the Onshape URL.\n"
            "Expected: https://cad.onshape.com/documents/<did>/w/<wid>/e/<eid>\n"
            f"Got:      {url}"
        )
    return base, m.group("did"), m.group("wvm"), m.group("wvm_id"), m.group("eid")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    cfg = load_config()
    access_key = cfg.get("access_key") or os.environ.get("ONSHAPE_ACCESS_KEY", "")
    secret_key = cfg.get("secret_key") or os.environ.get("ONSHAPE_SECRET_KEY", "")
    if not access_key or not secret_key:
        sys.exit("ERROR: Credentials not found (see .onshape file).")

    args  = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = sys.argv[1:]
    if not args:
        sys.exit("Usage: python onshape_export_gltf.py <assembly-url> [output.glb]")

    def flag_value(name: str) -> Optional[str]:
        for i, a in enumerate(flags):
            if a == name and i + 1 < len(flags):
                return flags[i + 1]
            if a.startswith(name + "="):
                return a.split("=", 1)[1]
        return None

    base_url, did, wvm, wvm_id, eid = parse_onshape_url(args[0].strip())
    out_path = args[1] if len(args) > 1 else "exploded_view.glb"

    params = {}
    if flag_value("--angle-tolerance"):
        params["angleTolerance"] = flag_value("--angle-tolerance")
    if flag_value("--chord-tolerance"):
        params["chordTolerance"] = flag_value("--chord-tolerance")

    creds   = base64.b64encode(f"{access_key}:{secret_key}".encode()).decode()
    query   = urlencode(params, quote_via=quote)
    url     = (f"{base_url}/api/{API_VERSION}/assemblies"
               f"/d/{did}/{wvm}/{wvm_id}/e/{eid}/gltf"
               + (f"?{query}" if query else ""))

    log.info("Exporting glTF from: %s", url)

    # Ask for binary glTF first; fall back to JSON glTF if the deployment
    # only serves that.
    for accept, kind in (("model/gltf-binary", "glb"),
                         ("model/gltf+json",   "gltf")):
        r = requests.get(
            url,
            headers={"Authorization": f"Basic {creds}", "Accept": accept},
            timeout=300,
        )
        if r.status_code == 406:
            log.info("Server declined Accept: %s — trying next.", accept)
            continue
        if not r.ok:
            log.error("HTTP %s\n%s", r.status_code, r.text[:600])
            r.raise_for_status()
        content = r.content
        is_glb  = content[:4] == b"glTF"
        if kind == "glb" and not is_glb:
            log.info("Response was not binary glTF — saving as JSON .gltf.")
            kind = "gltf"
        if kind == "gltf" and not out_path.lower().endswith(".gltf"):
            root, _ = os.path.splitext(out_path)
            out_path = root + ".gltf"
        with open(out_path, "wb") as f:
            f.write(content)
        log.info("Wrote %s  (%.1f MB, %s)", out_path,
                 len(content) / 1e6, "binary GLB" if is_glb else "JSON glTF")
        if kind == "gltf":
            log.warning(
                "JSON glTF may reference external buffers; the annotator "
                "handles embedded buffers only. Prefer GLB if available."
            )
        return

    sys.exit("ERROR: Server rejected both glTF Accept types.")


if __name__ == "__main__":
    main()
