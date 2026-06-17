# Onshape Exploded View → Annotator

Turn an Onshape assembly's **exploded view** into an interactive, annotated 3D
model you can orbit, balloon, and share as a single self-contained web page —
a faster alternative to drafting assembly drawings.

The pipeline is two steps, driven from a one-page dashboard:

1. **Build exploded model** — a handful of read-only API calls fetch the
   assembly geometry, the exploded-view transforms, and the BOM, then the
   explode is baked locally into a `.glb`. Nothing is created or modified in
   your Onshape document, and the call count is constant (~5) regardless of
   how many parts the assembly has.
2. **Open in annotator** — the model and parts list load in a browser viewer
   where you place numbered balloon callouts and publish a shareable file.

---

## What's in the folder

These three files are the tool, and must sit in the **same directory**:

| File | Purpose |
|------|---------|
| `dashboard.py` | Local control panel — the easy way to run everything. |
| `onshape_build_exploded.py` | Fetches geometry + transforms + BOM and bakes the exploded `.glb` locally. |
| `exploded_view_annotator.html` | The 3D viewer / annotator. |
| `.onshape` | Your API credentials (you create this — see below). |

You may also have older scripts alongside (`onshape_explode_to_assembly.py`,
`onshape_export_gltf.py`, `onshape_export_bom.py`, `dump_*.py`) from an earlier
version of the pipeline that built the exploded layout inside Onshape. They are
no longer used by the dashboard and can be deleted — the old approach consumed
roughly two API calls *per part*, which exhausts Onshape's API allowance on
larger assemblies.

On startup the dashboard prints a `WARNING: missing …` line for anything it
can't find next to it, so check the launch console is clean before clicking.

---

## Prerequisites

- **Python 3.9+** ([python.org](https://www.python.org/downloads/); on Windows,
  tick *"Add Python to PATH"* during install).
- **The `requests` library:**
  ```
  pip install requests
  ```
  (The dashboard server itself is standard-library only, but the build script
  it calls needs `requests`.)
- **An internet connection while using the annotator.** The viewer loads its 3D
  engine from a CDN at runtime, so it needs to be online — this applies both in
  the dashboard flow and when opening a published file.

---

## Get your Onshape API keys

Each person uses their **own** keys (don't share someone else's — see the
security note at the end).

1. Sign in at the Onshape **Developer Portal**:
   <https://dev-portal.onshape.com/keys>
2. Create an **API key** with **Read** scope. (The current pipeline only reads;
   it never writes to your documents.)
3. Copy the **Access key** and **Secret key** — the secret is shown only once.

Create a file named `.onshape` (no extension) in the project folder:

```ini
access_key = PASTE_YOUR_ACCESS_KEY
secret_key = PASTE_YOUR_SECRET_KEY
```

> On Windows, save it as `.onshape` (if Explorer adds `.txt`, rename it). The
> script also accepts the env vars `ONSHAPE_ACCESS_KEY` / `ONSHAPE_SECRET_KEY`
> instead of the file.

---

## Quick start

From the project folder:

```
python dashboard.py
```

Your browser opens the control panel. Then:

1. Paste your **source assembly URL** — the assembly that has the exploded
   view, copied straight from the browser while that tab is open. It must be a
   workspace URL (`/w/` in the path). The assembly needs at least one exploded
   view; the first one is used unless you pin a specific one (see options).
2. Click **Build exploded model**. The log streams progress; the key line is
   `Matched X / Y occurrence(s)` — X = Y with no warnings means every part was
   placed at its exploded position.
3. Click **Open in annotator**.

### Using the annotator

- **Navigation (both modes):** right-drag orbit · middle-drag pan · scroll zoom.
- **Place a balloon:** click the **◎** mode button, then left-click any part.
  Balloon numbers follow the BOM; names and part numbers fill in automatically.
  A part that can't be identified against the BOM shows a dashed **?** balloon
  rather than a wrong number.
- **Edit / reorder / delete** balloons in the right-hand panel; **FOCUS** flies
  the camera to a balloon.
- **Save notes** writes a small `.json` you can reload later (it embeds the
  parts list, so numbering survives reloads).
- **Publish** bakes the model + balloons + camera view into one self-contained
  HTML file to share (see *Sharing results*).

---

## Manual use (without the dashboard)

```bash
python onshape_build_exploded.py "<source-assembly-url>"
#   → exploded_view.glb + parts.json
```

Then open `exploded_view_annotator.html` in a browser, drop in
`exploded_view.glb`, then drop in `parts.json`.

Options:

```
--glb out.glb        output model path   (default exploded_view.glb)
--parts parts.json   output parts list   (default parts.json)
--bom-indented       include parts nested in sub-assemblies as their own
                     BOM rows (item numbers like 1.1, 1.2 …)
```

The script also has diagnostic modes (`--diagnose`, `--probe-…`) used during
development; they make a few read calls, print analysis, and write nothing.

---

## `.onshape` options

Only `access_key` and `secret_key` are required. Optional:

```ini
access_key            = XXXXXXXXXXXXXXXXXXXXXXXX
secret_key            = YYYYYYYYYYYYYYYYYYYYYYYY

# Pin to a specific exploded view (otherwise the first one is used):
# exploded_view_id    = M7/LVDNHe4LtEu/QV
```

Dashboard port (if 8765 is busy): set the env var `EV_DASHBOARD_PORT`, e.g.
`EV_DASHBOARD_PORT=9000 python dashboard.py`.

---

## Sharing results

Two different kinds of sharing:

- **Share the finished view only.** Click **Publish** in the annotator and send
  the single downloaded HTML file. The recipient needs only a browser and an
  internet connection — no Python, no scripts, no API keys. They can orbit the
  model and read every balloon, but can't edit. (The file is roughly 1.3× the
  size of the `.glb`, since the model is embedded.)
- **Share the toolkit so a colleague can make their own.** Send the three
  project files. They install Python + `requests` and create their **own**
  `.onshape` with **their own** API keys (see above).

---

## Troubleshooting

- **Build says `Matched X / Y` with X < Y, or warns about unmatched parts.**
  Some parts couldn't be paired between the geometry and the assembly's
  occurrence list, and will sit at their assembled position. Run
  `python onshape_build_exploded.py "<url>" --probe-occ-vs-gltf` and share the
  output — it lists exactly which parts and why.
- **Parts sitting un-exploded at the centre of the model.** Same cause as
  above — check the build log rather than trusting the picture.
- **Some balloons show `?` instead of a number.** That part's name didn't match
  any BOM row. Check `parts.json` looks right; if names/numbers are blank, the
  BOM columns may differ on your document — run the build and inspect, or ask
  for the `--dump`-style BOM diagnostics.
- **Parts nested in sub-assemblies missing from the BOM.** Tick the
  sub-assemblies checkbox on the dashboard (or pass `--bom-indented`).
- **`ModuleNotFoundError: requests`.** Run `pip install requests`.
- **`WARNING: missing …` at dashboard startup.** The build script or annotator
  isn't in the same folder as `dashboard.py`. Put all the files together.
- **Annotator looks broken / no 3D.** Check you're online (it needs the CDN).
- **Port already in use.** Set `EV_DASHBOARD_PORT` to a free port.
- **URL is a version (`/v/`) or microversion (`/m/`).** Use the workspace URL
  (`/w/`) copied from the open assembly tab.

---

## Notes & limitations

- The pipeline is **read-only**: it never creates, modifies, or deletes
  anything in your Onshape documents.
- API usage is constant per build (~5 read calls), independent of part count —
  comfortably inside Onshape's API allowance even with frequent use.
- Composite parts (e.g. imported bearings/seals) are supported; the assembly
  definition is fetched with `includeNonSolids=true` specifically so they are
  enumerated. If an assembly contains genuine non-solid instances (construction
  surfaces etc.), the build may log harmless "unmatched occurrence" warnings
  for them.
- Exploded views are non-destructive overlays in Onshape; this tool reads the
  exploded transforms and bakes them into the exported geometry only.

---

## Security note

Your `.onshape` file holds API keys that act as **you** on Onshape. Treat it
like a password: don't commit it to shared repos, and don't send it to others.
If a key is ever exposed, revoke it at <https://dev-portal.onshape.com/keys>
and generate a new one.
