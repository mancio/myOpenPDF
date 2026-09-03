# Technical Specification: PDF Studio

Implementation contract for the plan in [PLAN-web-pdf-editor.md](PLAN-web-pdf-editor.md).
Written to be handed to a coding agent. Anything not specified here is the implementer's
choice, but nothing specified here should be changed silently.

- Runtime: FastAPI backend on `127.0.0.1`, React frontend, single user, no auth.
- Licence: AGPL-3.0 (PyMuPDF dependency).
- Browsers: desktop evergreen only.

---

## 1. Guiding principles

1. **The backend owns the PDF.** The frontend never mutates PDF bytes. Every change is an
   op sent to the API.
2. **Non-destructive.** The uploaded file is never overwritten. Edits are an ordered op
   log; the current PDF is derived by replaying ops onto the original.
3. **Deterministic on one machine.** Given the same original, the same op log and the
  same seed on the same machine (same OS, Python and locked dependency versions), output
  is byte-stable apart from PDF timestamps.
4. **Stable identity.** Pages are addressed by UUID, never by index, because reorder and
   delete change indices.
5. **Fail loudly, never silently corrupt.** An op that cannot be applied returns 4xx and
   leaves the log untouched.

---

## 2. Environment and toolchain

### 2.1 Versions

| Component | Version |
| --- | --- |
| Python | 3.13 |
| Node | 22 LTS |
| Package managers | `uv` (Python), `pnpm` (Node) |

Resolve exact dependency versions at install time; the ranges below are the intent.

`backend/pyproject.toml` dependencies:

```toml
[project]
name = "pdf-studio-backend"
requires-python = ">=3.13"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.34",
  "python-multipart>=0.0.20",
  "pydantic>=2.10",
  "pydantic-settings>=2.7",
  "sqlmodel>=0.0.22",
  "pymupdf>=1.26",
  "pillow>=11",
  "numpy>=2",
]

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.25", "httpx>=0.28", "ruff>=0.9", "mypy>=1.14"]
```

`frontend/package.json` dependencies: `react`, `react-dom` (19.x), `pdfjs-dist` (5.x),
`fabric` (6.x), `zustand` (5.x), `immer`, `@tanstack/react-query` (5.x), `react-router`
(7.x), `tailwindcss` (4.x), `clsx`, `lucide-react`.
Dev: `vite` (7.x), `typescript` (5.7+), `vitest`, `@playwright/test`, `eslint`, `prettier`.

### 2.2 Developer commands (Windows PowerShell)

```powershell
# backend
cd backend
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# frontend
cd frontend
pnpm install
pnpm dev            # Vite on 5173, proxies /api to 127.0.0.1:8000

# checks
cd backend;  uv run ruff check . ; uv run mypy app ; uv run pytest
cd frontend; pnpm lint ; pnpm test ; pnpm exec playwright test
```

Vite proxy in `vite.config.ts`:

```ts
server: {
  port: 5173,
  proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false } },
}
```

### 2.3 Settings

`backend/app/config.py`, Pydantic `BaseSettings`, prefix `PDFSTUDIO_`:

| Setting | Default | Notes |
| --- | --- | --- |
| `store_root` | `./data` | Documents, renders, assets |
| `db_path` | `./data/pdfstudio.sqlite3` | |
| `bind_host` | `127.0.0.1` | Must not default to `0.0.0.0` |
| `allowed_origins` | `["http://localhost:5173","http://127.0.0.1:5173"]` | CORS allowlist |
| `max_upload_bytes` | `200_000_000` | |
| `max_pages` | `2000` | |
| `max_render_dpi` | `600` | |
| `job_timeout_seconds` | `900` | |
| `render_cache_max_bytes` | `2_000_000_000` | LRU eviction |

---

## 3. Storage and data model

### 3.1 Disk layout

```
data/
  pdfstudio.sqlite3
  documents/<doc_uuid>/
    original.pdf                 immutable
    derived/<version>.pdf        cached replay result, version = op cursor
    thumbs/<version>/<page_uuid>.webp
    assets/<asset_uuid>.<ext>    signatures, stamps, imported images
  presets.json                   optional export of user presets
  tmp/                           in-progress job output, fsync + atomic rename
```

`version` is an integer equal to the op cursor. `derived/0.pdf` is a copy-on-read of the
original. Keep at most the newest 5 derived files per document plus version 0; evict the
rest.

### 3.2 SQLite schema

```sql
CREATE TABLE document (
  id            TEXT PRIMARY KEY,          -- uuid4
  title         TEXT NOT NULL,
  original_name TEXT NOT NULL,
  sha256        TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL,
  page_count    INTEGER NOT NULL,
  cursor        INTEGER NOT NULL DEFAULT 0, -- ops applied; also the version number
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE op (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id TEXT NOT NULL REFERENCES document(id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,            -- 1-based position in the log
  kind        TEXT NOT NULL,
  payload     TEXT NOT NULL,               -- JSON
  created_at  TEXT NOT NULL,
  UNIQUE (document_id, seq)
);

CREATE TABLE page_ref (                    -- stable page identity
  document_id TEXT NOT NULL REFERENCES document(id) ON DELETE CASCADE,
  page_uuid   TEXT NOT NULL,
  origin      TEXT NOT NULL,               -- 'original' | 'blank' | 'imported'
  origin_index INTEGER,                    -- index in the source doc, null for blank
  PRIMARY KEY (document_id, page_uuid)
);

CREATE TABLE preset (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  params     TEXT NOT NULL,                -- JSON ScanParams
  builtin    INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE job (
  id          TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  kind        TEXT NOT NULL,               -- 'scan' | 'export' | 'ocr'
  status      TEXT NOT NULL,               -- queued|running|done|error|cancelled
  progress    REAL NOT NULL DEFAULT 0,
  message     TEXT,
  result_path TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
```

Redo is supported by keeping ops with `seq > cursor` in the table. Applying a new op while
`cursor < max(seq)` deletes all ops with `seq > cursor` first (standard truncate-on-branch).

---

## 4. The op log

### 4.1 Op kinds

| kind | payload |
| --- | --- |
| `page.reorder` | `{ order: PageUuid[] }` full new order |
| `page.rotate` | `{ pages: PageUuid[], delta: 90 \| 180 \| 270 }` |
| `page.delete` | `{ pages: PageUuid[] }` |
| `page.duplicate` | `{ page: PageUuid, newUuid: PageUuid, after: PageUuid }` |
| `page.insert_blank` | `{ newUuid: PageUuid, after: PageUuid \| null, width: float, height: float }` |
| `page.import` | `{ assetId: str, pages: [int,int] \| null, after: PageUuid \| null, newUuids: PageUuid[] }` |
| `annot.add` | `{ annot: Annotation }` |
| `annot.update` | `{ annot: Annotation }` |
| `annot.delete` | `{ id: str }` |
| `form.set` | `{ page: PageUuid, field: str, value: str \| bool \| int }` |
| `redact.apply` | `{ page: PageUuid, rects: Rect[], fill: [r,g,b] \| null }` |
| `text.replace` | `{ page: PageUuid, rect: Rect, old: str, new: str, font: FontSpec }` |
| `scan.apply` | `{ params: ScanParams }` whole document, terminal-ish |
| `doc.flatten` | `{ annots: bool, widgets: bool }` |

Rules:

- `scan.apply` and `doc.flatten` rasterise or bake. After them, page-level text ops are
  rejected with `409 OP_NOT_APPLICABLE`.
- `scan.apply` is irreversible in UX: the frontend must require explicit confirmation,
  show a one-line warning that the result is image-only, and stop exposing undo/redo for
  that document session after commit.
- Ops are pure data. Replay must not depend on wall-clock time or unseeded randomness.
- `annot.*` ops carry the full annotation object, not a diff, so replay is trivial.
- Annotation identity lives only in `annot.id` and `annot.page`; do not duplicate these at
  top level in op payloads.
- If `ScanParams.seed` is null, backend must generate an integer seed and persist that
  effective value inside the stored `scan.apply` op payload.

### 4.2 Replay

```python
def build(doc_id: str, upto: int) -> Path:
    cached = derived_path(doc_id, upto)
    if cached.exists():
        return cached
    pdf = pymupdf.open(original_path(doc_id))
    state = PageState.from_original(pdf)      # page_uuid -> index mapping
    for op in ops(doc_id, seq<=upto):
        apply_op(pdf, state, op)
    tmp = tmp_path()
    pdf.save(tmp, garbage=4, deflate=True, clean=True)
    tmp.replace(cached)                        # atomic
    return cached
```

Replay from scratch is acceptable for correctness. Optimise later by starting from the
newest cached version `<= upto` only if every intervening op is forward-applicable.

---

## 5. Coordinate systems

This is the most common source of bugs. Fix it once.

- **Canonical space**: PDF points (1/72 inch), origin **top-left**, y increasing
  **downward**, relative to the page's *unrotated* MediaBox.
- All geometry in the API — annotations, redaction rects, text rects — uses canonical space.
- PyMuPDF `Rect` uses top-left origin already. To go canonical -> displayed, multiply by
  `page.rotation_matrix`; the inverse is `page.derotation_matrix`. Store canonical, convert
  at apply time, so a `page.rotate` op never needs to rewrite annotation geometry.
- pdf.js: `const vp = page.getViewport({ scale })` yields CSS pixels, top-left origin, with
  rotation applied. Convert with `vp.convertToViewportPoint(x, y)` and
  `vp.convertToPdfPoint(x, y)` — but note pdf.js PDF space is bottom-left, so wrap it:

```ts
// canonical (top-left, points) -> viewport CSS px
export function canonicalToViewport(p: Pt, vp: PageViewport, pageHeight: number): Pt {
  const [x, y] = vp.convertToViewportPoint(p.x, pageHeight - p.y);
  return { x, y };
}
export function viewportToCanonical(p: Pt, vp: PageViewport, pageHeight: number): Pt {
  const [x, y] = vp.convertToPdfPoint(p.x, p.y);
  return { x, y: pageHeight - y };
}
```

- Never store device pixels or `devicePixelRatio`-scaled values.
- `Rect` is `[x0, y0, x1, y1]` with `x0 <= x1`, `y0 <= y1`. Normalise on receipt.

---

## 6. API contract

Base path `/api`. JSON in, JSON out, except file upload/download. All ids are UUID4 strings.

### 6.1 Documents

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| POST | `/documents` | multipart `file`, optional `title` | `Document` |
| GET | `/documents` | `?q=&limit=&offset=` | `PageDocuments` |
| GET | `/documents/{id}` | | `Document` |
| PATCH | `/documents/{id}` | `{title}` | `Document` |
| DELETE | `/documents/{id}` | | `204` |
| GET | `/documents/{id}/file` | `?version=` | `application/pdf` |
| GET | `/documents/{id}/pages` | | `PageInfo[]` |
| GET | `/documents/{id}/pages/{pageUuid}/thumb` | `?dpi=110` | `image/webp` |
| GET | `/documents/{id}/pages/{pageUuid}/text` | | `TextBlock[]` |
| POST | `/documents/{id}/search` | `{query, caseSensitive}` | `SearchHit[]` |

### 6.2 Editing

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| POST | `/documents/{id}/ops` | `Op` | `OpResult` |
| GET | `/documents/{id}/ops` | | `OpLogResponse` |
| POST | `/documents/{id}/undo` | | `OpResult` |
| POST | `/documents/{id}/redo` | | `OpResult` |
| POST | `/documents/{id}/assets` | multipart `file` | `{assetId, width, height}` |
| GET | `/documents/{id}/forms` | | `FormField[]` |

`OpResult`:

```json
{ "cursor": 7, "canUndo": true, "canRedo": false, "pageCount": 12, "version": 7 }
```

The frontend reacts to `version` by invalidating the pdf.js document and thumbnails.

Irreversible scan rule: if the latest applied op is `scan.apply`, backend must return
`409 OP_NOT_APPLICABLE` for `/documents/{id}/undo` and `/documents/{id}/redo`.

### 6.3 Scan

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| GET | `/scan/presets` | | `Preset[]` |
| POST | `/scan/presets` | `{name, params}` | `Preset` |
| DELETE | `/scan/presets/{id}` | | `204` |
| POST | `/documents/{id}/scan/preview` | `{pageUuid, params, previewDpi}` | `image/webp` |
| POST | `/documents/{id}/scan` | `{params, mode}` | `Job` |

`mode`: `"in_place"` appends a `scan.apply` op to the document and is irreversible in UX;
`"export"` produces a downloadable file without touching the op log. Preview is capped at
`previewDpi <= 150` and must run the identical stage chain as the full render, only at
lower DPI, so what you see is what you save.

### 6.4 Jobs and export

| Method | Path | Response |
| --- | --- | --- |
| GET | `/jobs/{id}` | `Job` |
| GET | `/jobs/{id}/events` | `text/event-stream`, events `progress`, `done`, `error` |
| POST | `/jobs/{id}/cancel` | `202` |
| GET | `/jobs/{id}/result` | file download |
| POST | `/documents/{id}/export` | `{format: "pdf"\|"png"\|"jpeg", flatten, pages, dpi}` | `Job` |

SSE payload: `{"progress":0.42,"page":13,"pageCount":31,"message":"rendering"}`.

Export result contract:

- `format="pdf"`: always one PDF file (`application/pdf`).
- `format="png"` or `"jpeg"` with exactly one selected page: return that single image
  file (`image/png` or `image/jpeg`).
- `format="png"` or `"jpeg"` with multiple selected pages: return one ZIP archive
  (`application/zip`) containing `page-0001.ext`, `page-0002.ext`, ... in page order.

### 6.5 Error format

```json
{ "error": { "code": "OP_NOT_APPLICABLE", "message": "human readable", "detail": {} } }
```

Codes: `NOT_FOUND`, `INVALID_PDF`, `ENCRYPTED_PDF`, `TOO_LARGE`, `TOO_MANY_PAGES`,
`OP_NOT_APPLICABLE`, `PAGE_NOT_FOUND`, `FIELD_NOT_FOUND`, `TEXT_NOT_FOUND`, `JOB_FAILED`,
`UNSUPPORTED_FEATURE`. HTTP status maps sensibly (404, 409, 413, 422, 500).

---

## 7. Shared types

Define once in Pydantic, generate the TS types from the OpenAPI schema
(`pnpm exec openapi-typescript http://127.0.0.1:8000/openapi.json -o src/lib/api.d.ts`)
so they cannot drift.

```python
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

PageUuid = str
Rect = tuple[float, float, float, float]          # canonical, top-left origin
Color = tuple[float, float, float]                # 0..1 RGB

class PageInfo(BaseModel):
    uuid: PageUuid
    index: int
    width: float                                   # points, unrotated
    height: float
    rotation: Literal[0, 90, 180, 270]
    has_text: bool
    label: str | None

class Annotation(BaseModel):
    id: str
    page: PageUuid
    kind: Literal["ink","highlight","underline","strikeout","squiggly","rect",
                  "ellipse","line","arrow","polygon","freetext","note","stamp","image"]
    rect: Rect | None = None
    quads: list[Rect] | None = None                # text markup
    strokes: list[list[tuple[float, float]]] | None = None   # ink, canonical points
    points: list[tuple[float, float]] | None = None
    text: str | None = None
    color: Color | None = None
    fill: Color | None = None
    opacity: float = 1.0
    width: float = 1.0                             # stroke width in points
    font: FontSpec | None = None
    asset_id: str | None = None                    # stamp / image / signature
    rotation: float = 0.0
    created_at: datetime

class FontSpec(BaseModel):
    family: str = "helv"                           # PyMuPDF base-14 alias or bundled font id
    size: float = 11.0
    bold: bool = False
    italic: bool = False
    color: Color = (0, 0, 0)
    align: Literal["left","center","right","justify"] = "left"

class Document(BaseModel):
    id: str
    title: str
    original_name: str
    size_bytes: int
    page_count: int
    cursor: int
    version: int
    created_at: datetime
    updated_at: datetime

class PageDocuments(BaseModel):
    items: list[Document]
    total: int
    limit: int
    offset: int

class TextBlock(BaseModel):
    page: PageUuid
    block_index: int
    rect: Rect
    text: str

class SearchHit(BaseModel):
    page: PageUuid
    rects: list[Rect]
    preview: str

class FormField(BaseModel):
    page: PageUuid
    name: str
    field_type: Literal["text","checkbox","radio","combo","list","signature"]
    value: str | bool | int | None
    rect: Rect | None = None

class Preset(BaseModel):
    id: str
    name: str
    params: dict[str, Any]                     # validated against ScanParams at API boundary
    builtin: bool
    created_at: datetime

class Job(BaseModel):
    id: str
    document_id: str
    kind: Literal["scan","export","ocr"]
    status: Literal["queued","running","done","error","cancelled"]
    progress: float
    message: str | None = None
    result_path: str | None = None
    created_at: datetime
    updated_at: datetime

class StoredOp(BaseModel):
    seq: int
    kind: str
    payload: dict[str, Any]
    created_at: datetime

class OpLogResponse(BaseModel):
    cursor: int
    ops: list[StoredOp]

class OpResult(BaseModel):
    cursor: int
    canUndo: bool
    canRedo: bool
    pageCount: int
    version: int
    warnings: list[str] = Field(default_factory=list)

class ExportRequest(BaseModel):
    format: Literal["pdf","png","jpeg"]
    flatten: bool = False
    pages: list[PageUuid] | None = None
    dpi: int = 200

# Op is a discriminated union keyed by `kind`; each kind has a dedicated payload model.
```

---

## 8. Scan pipeline specification

`backend/app/scan/pipeline.py`. This is the feature that must feel right, so it is
specified stage by stage. Stage order matters and must not be rearranged.

### 8.1 Parameters

```python
class ScanParams(BaseModel):
    seed: int | None = None                  # None -> backend generates and persists effective seed
    dpi: int = Field(200, ge=72, le=600)

    color_mode: Literal["gray", "color", "bw"] = "gray"
    paper_tint: str = "#FFFFFF"              # base paper colour
    gamma: float = Field(1.0, ge=0.4, le=2.5)
    brightness: float = Field(1.0, ge=0.5, le=1.5)
    contrast: float = Field(1.0, ge=0.5, le=2.0)
    jitter: float = Field(0.03, ge=0.0, le=0.2)   # per-page variation of the two above

    skew_deg: float = Field(0.0, ge=0.0, le=3.0)  # max absolute random skew per page
    offset_pct: float = Field(0.0, ge=0.0, le=2.0)  # random translation, % of page

    sharpen: float = Field(0.0, ge=0.0, le=2.0)   # unsharp amount
    blur_sigma: float = Field(0.0, ge=0.0, le=3.0)
    noise_sigma: float = Field(0.0, ge=0.0, le=40.0)
    noise_mono: bool = True                        # same noise on all channels

    texture: Literal["none","fibre","recycled","aged"] = "none"
    texture_opacity: float = Field(0.15, ge=0.0, le=0.6)
    dust: float = Field(0.0, ge=0.0, le=1.0)
    vignette: float = Field(0.0, ge=0.0, le=0.6)
    edge_shadow: Literal["none","left","right","both"] = "none"
    edge_shadow_strength: float = Field(0.3, ge=0.0, le=1.0)
    border_mm: float = Field(0.0, ge=0.0, le=10.0)  # scanner lid border
    border_color: str = "#1A1A1A"

    bw_threshold: int = Field(128, ge=0, le=255)
    bw_dither: bool = True

    jpeg_quality: int = Field(75, ge=20, le=98)
    downsample: float = Field(1.0, ge=0.25, le=1.0)  # resample after effects, before encode
```

### 8.2 Stage chain

Operate on a float32 numpy array in 0..255, RGB, and convert once at the end.

| # | Stage | Implementation |
| --- | --- | --- |
| 1 | Render | `page.get_pixmap(matrix=Matrix(dpi/72, dpi/72), colorspace=csRGB, alpha=True)` |
| 2 | Paper base | Alpha-composite rendered pixels over a solid `paper_tint` canvas, then continue in RGB |
| 3 | Gamma | `out = 255 * (x / 255) ** (1 / gamma)` |
| 4 | Contrast | PIL-compatible: `m = mean(luma(x)); out = m + c * (x - m)` where `c` is `contrast * (1 ± jitter)` |
| 5 | Brightness | `out = x * brightness * (1 ± jitter)` |
| 6 | Sharpen | Unsharp mask: `out = x + amount * (x - gaussian(x, sigma=1.0))` |
| 7 | Geometry | Rotate by `uniform(-skew_deg, skew_deg)`, bicubic, fill with paper tint; then translate by `offset_pct` of page size |
| 8 | Blur | Gaussian, `blur_sigma` in pixels at the working DPI (scale it: `sigma * dpi / 200`) |
| 9 | Noise | `out = x + normal(0, noise_sigma, shape)`; one channel broadcast if `noise_mono` |
| 10 | Dust | `n = int(dust * 400 * area_in_A4_units)` specks, radius `uniform(0.4, 2.2)` px, 70% dark 30% light |
| 11 | Texture | Tile `textures/{name}.png`, blend with `multiply` at `texture_opacity` |
| 12 | Vignette | `r = hypot((x-w/2)/(w/2), (y-h/2)/(h/2)) / sqrt(2); out = x * (1 - vignette * r**2)` |
| 13 | Edge shadow | Horizontal exponential falloff on the chosen side(s), width ~4% of page |
| 14 | Border | Draw `border_color` frame of `border_mm` around the content |
| 15 | Colour mode | `gray`: luma `0.299R+0.587G+0.114B`. `bw`: threshold at `bw_threshold`, Floyd-Steinberg if `bw_dither`. `color`: unchanged |
| 16 | Downsample | Lanczos resize by `downsample` |
| 17 | Encode | `gray`/`color` -> JPEG at `jpeg_quality`; `bw` -> 1-bit PNG |
| 18 | Reassemble | New page at the **original page size in points**, `page.insert_image(page.rect, stream=buf)` |

Per-page RNG: `rng = numpy.random.default_rng([seed, page_index])`. This gives page-to-page
variation while staying fully reproducible on one machine. If request seed is null,
generate one integer seed, persist it in the op (or export job metadata), and echo it in
the result so a good-looking scan can be recreated.

Output document: `doc.set_metadata({})` to drop the source metadata, and do not copy the
original's XMP. The result is image-only by construction.

### 8.3 Built-in presets

| Preset | Key values |
| --- | --- |
| `subtle` | dpi 300, gray, skew 0.2, contrast 1.05, noise 4, blur 0.2, jpeg 90 |
| `medium` | dpi 200, gray, skew 0.6, contrast 1.18, noise 10, blur 0.4, vignette 0.1, texture fibre 0.1, jpeg 75 |
| `heavy` | dpi 150, gray, skew 1.2, contrast 1.35, noise 20, blur 0.7, vignette 0.2, dust 0.4, texture aged 0.25, jpeg 55 |
| `photocopy` | dpi 200, bw + dither, contrast 1.6, noise 12, edge_shadow both, border 3mm |
| `fax` | dpi 100, bw no dither, contrast 1.8, blur 0.3, noise 6 |
| `archive` | dpi 200, color, paper_tint `#F7F0DF`, texture aged 0.3, vignette 0.25, dust 0.6, jpeg 65 |

`subtle` / `medium` / `heavy` should land close to the current `scan_effect.py` output;
close enough that they are recognisable, not pixel-identical.

### 8.4 UI tuning panel

Sliders grouped as Paper, Optics, Noise, Output; a preset dropdown with "Save as preset";
a seed field with a dice button; a live preview of the current page that re-requests at
most every 250 ms (debounced, previous request aborted). Preview at 110 DPI.

---

## 9. PyMuPDF operation reference

Use these calls; do not invent alternatives.

```python
# page ops
doc.select(new_index_order)                   # reorder + delete in one shot
doc.move_page(from_index, to_index)
doc.delete_page(index)
doc.fullcopy_page(index, to_index)            # duplicate
doc.new_page(pno, width=w, height=h)
doc.insert_pdf(src, from_page=a, to_page=b, start_at=i)
page.set_rotation((page.rotation + delta) % 360)

# annotations
page.add_ink_annot(strokes)                   # list[list[Point]]
page.add_highlight_annot(quads)
page.add_underline_annot(quads)
page.add_strikeout_annot(quads)
page.add_rect_annot(rect); page.add_circle_annot(rect)
page.add_line_annot(p1, p2)
page.add_polygon_annot(points)
page.add_freetext_annot(rect, text, fontsize=..., fontname=..., text_color=..., fill_color=...)
page.add_text_annot(point, text)              # sticky note
page.add_stamp_annot(rect, stamp=...)         # or insert_image for custom stamps/signatures
annot.set_colors(stroke=..., fill=...); annot.set_opacity(a); annot.set_border(width=w)
annot.update()                                # required after every change

# images and signatures
page.insert_image(rect, stream=png_bytes, keep_proportion=True, overlay=True)

# forms
for w in page.widgets():
    if w.field_name == name:
        w.field_value = value
        w.update()

# redaction - the only acceptable method
page.add_redact_annot(rect, fill=fill or None)
page.apply_redactions(
    images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
    graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
    text=pymupdf.PDF_REDACT_TEXT_REMOVE,
)

# flatten
doc.bake(annots=True, widgets=True)

# save
doc.save(path, garbage=4, deflate=True, clean=True)
```

Guard optional constants with `getattr(pymupdf, "PDF_REDACT_...", default)` so a minor
version bump cannot break startup.

### 9.1 Text replacement (`text.replace`)

Best-effort, single span, explicitly not reflow.

1. `hits = page.search_for(old)`; require exactly one hit inside the supplied `rect`,
   otherwise `422 TEXT_NOT_FOUND`.
2. Read the span from `page.get_text("dict")` to recover `font`, `size`, `color`, `origin`.
3. Map the original font name to a usable font: exact match if it is a base-14 alias,
   otherwise the closest bundled open font (`DejaVuSans`, `DejaVuSerif`, `DejaVuSansMono`)
   preserving serif/sans and bold/italic. Record the substitution in the op result so the
   UI can warn.
4. Redact the span rect with `fill=None` (removes text without painting a box).
5. `page.insert_textbox(expanded_rect, new_text, fontname=..., fontsize=..., color=...,
   align=...)`. If it returns a negative value the text does not fit: retry once at 95%
   font size, then fail with `422 UNSUPPORTED_FEATURE`.

Expose the substitution and any auto-shrink in `OpResult.warnings: string[]`.

---

## 10. Frontend architecture

### 10.1 pdf.js setup

```ts
import * as pdfjs from 'pdfjs-dist';
pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url
).toString();

const doc = await pdfjs.getDocument({
  url: `/api/documents/${id}/file?version=${version}`,
  isEvalSupported: false,
  enableXfa: false,
}).promise;
```

- The `version` query string is the cache key. When `OpResult.version` changes, destroy the
  old `PDFDocumentProxy` (`doc.destroy()`) and load the new URL. Never mutate in place.
- Virtualise pages: render only what is in the viewport plus one page either side. Keep a
  small LRU of rendered canvases and call `renderTask.cancel()` when a page scrolls out.
- Render at `viewport.scale * devicePixelRatio`, then set CSS size to the logical size.
- Text layer from `page.getTextContent()` for selection and search highlighting.

### 10.2 Layer stack per page

```
z0  canvas          pdf.js raster
z1  text layer      transparent, selectable
z2  fabric canvas   annotations, in-progress drawing
z3  overlay HTML    handles, form field widgets, redaction boxes
```

Fabric objects carry `data.annotationId`. On `object:modified`, convert geometry back to
canonical points and POST an `annot.update` op, debounced 400 ms.

### 10.3 State

```ts
// zustand
interface EditorState {
  documentId: string | null;
  version: number;
  pages: PageInfo[];
  cursor: number; canUndo: boolean; canRedo: boolean;
  activeTool: ToolId;
  selection: string[];        // annotation ids
  zoom: number; pageIndex: number;
  scanParams: ScanParams; scanPreviewUrl: string | null;
  pendingOps: number;         // for the busy indicator
}
```

Undo/redo are server calls, not a client stack. Ctrl+Z maps to `POST /undo` except after an
in-place `scan.apply`, where undo/redo is intentionally hidden in UX for that session.
This keeps one source of truth and enforces the irreversible-scan UX decision.

### 10.4 Keyboard shortcuts

`Ctrl+O` open, `Ctrl+S` save/download, `Ctrl+Z` / `Ctrl+Shift+Z` undo/redo, `Ctrl+F` find,
`Ctrl+P` print, `+` / `-` zoom, `Ctrl+0` fit page, `Ctrl+1` actual size, `Delete` remove
selection, `Escape` cancel tool, `[` / `]` rotate selected pages.

---

## 11. Security requirements

Localhost does not mean unguarded. Implement all of these.

1. **Bind** `127.0.0.1` only. Reject a config that sets `0.0.0.0` unless
   `PDFSTUDIO_ALLOW_EXTERNAL=1` is set explicitly.
2. **CORS** allowlist only the two Vite origins. No `allow_origins=["*"]`.
3. **Path safety**: every filesystem path goes through

```python
def safe_path(root: Path, *parts: str) -> Path:
    p = (root / Path(*parts)).resolve()
    if not p.is_relative_to(root.resolve()):
        raise HTTPException(400, "invalid path")
    return p
```

   User-supplied filenames are never used as paths; only UUIDs are. The original name is
   stored in the database as data, and sanitised when echoed in `Content-Disposition`.
4. **Upload validation**: enforce `max_upload_bytes` while streaming (do not read into
  memory first), verify `%PDF-` appears within the first 1024 bytes, then open with
  PyMuPDF in a try/except and enforce `max_pages`; reject `doc.needs_pass` with
  `ENCRYPTED_PDF` unless a password is supplied.
5. **Resource caps**: reject `dpi * page_count` above a threshold; per-job timeout;
   cancellable jobs; bound the render cache with LRU eviction.
6. **pdf.js**: `isEvalSupported: false`, `enableXfa: false`, no PDF JavaScript execution,
   no auto-following of link annotations to non-http schemes.
7. **CSP** on the frontend: `default-src 'self'; script-src 'self'; object-src 'none';
   frame-ancestors 'none'`. No inline scripts.
8. **No shelling out.** Everything through PyMuPDF/Pillow. If OCR is added later, invoke
   Tesseract with an argument list, never a shell string, and only on paths the app owns.
9. **Redaction correctness is a security control**, not a cosmetic feature. It is covered by
   a mandatory test (section 12.2).
10. **Dependency hygiene**: `uv lock` and `pnpm-lock.yaml` committed; `pip-audit` and
    `pnpm audit` in CI.

---

## 12. Testing

### 12.1 Fixtures

`scripts/make_fixtures.py` generates every test document with PyMuPDF, using lorem text and
generated shapes. No real document ever enters the repo.

| Fixture | Contents |
| --- | --- |
| `text_1p.pdf` | Single page, headings and paragraphs |
| `text_30p.pdf` | Multi-page with page numbers |
| `mixed.pdf` | Text, a table, a raster image, a vector chart |
| `form.pdf` | AcroForm: text fields, checkbox, radio, dropdown |
| `rotated.pdf` | Pages at 0/90/180/270 |
| `sizes.pdf` | A4, Letter, A3 landscape in one file |
| `secret.pdf` | Contains the marker string `REDACTME-7F3A` |

### 12.2 Mandatory tests

| Area | Assertion |
| --- | --- |
| Op log | Apply N ops, undo N times, output hash equals version 0 hash |
| Op log | Undo then a new op truncates redo; `cursor` and `seq` stay consistent |
| Page identity | Reorder then rotate by uuid affects the intended page after reordering |
| Redaction | After redacting the marker, `page.get_text()` contains no `REDACTME-7F3A`, and raster output shows the region visually removed |
| Redaction | Marker absent from decoded PDF objects and streams (inspect all xrefs via PyMuPDF), not just raw file byte search |
| Scan | On the same machine and locked deps: same seed + params -> identical image bytes for every page |
| Scan | Output has the same page count and page sizes in points as the input |
| Scan | Output contains no extractable text |
| Scan | Preview at 110 DPI and full render at 300 DPI are perceptually similar (SSIM > 0.9 after resize) |
| Coordinates | An annotation placed at a known point round-trips through rotate 90 four times unchanged |
| Forms | Setting each field type persists and reads back |
| Text replace | Replacement renders inside the original rect; warnings list font substitution |
| Security | `../` in any path parameter returns 400 |
| Security | Upload of a 1-byte file, a non-PDF, and an oversized file each return the right code |
| API | Every endpoint has a happy path and a 4xx test |

### 12.3 E2E (Playwright)

Upload `mixed.pdf`, view it, rotate a page, add a highlight and an ink stroke, undo twice,
apply the `medium` scan preset, download the result, assert the downloaded file is a valid
PDF with the expected page count.

---

## 13. Performance targets

Measured on the development machine with `text_30p.pdf` and a 300-page stress fixture.

| Operation | Target |
| --- | --- |
| Upload + open 30 pages | < 1.5 s to first page painted |
| Page navigation render | < 120 ms per page at 100% zoom |
| Thumbnail strip, 30 pages | < 2 s, generated lazily |
| Structural op (rotate/reorder) round trip | < 400 ms for 30 pages |
| Scan preview refresh | < 500 ms at 110 DPI |
| Full scan, 30 pages at 200 DPI | < 20 s with visible progress |
| Memory, backend, 300-page scan | < 1 GB RSS, page-at-a-time processing |

Process pages one at a time and release pixmaps (`pix = None`) immediately; never hold the
whole document as arrays.

---

## 14. Definition of done per milestone

**M0** — Upload a PDF, see it in the library, `GET /documents/{id}/file` returns it,
SQLite schema migrated, `ruff`/`mypy`/`pytest`/`eslint`/`vitest` all green, README with the
dev commands.

**M1** — Continuous scroll viewer, zoom controls, fit modes, thumbnail sidebar, text
selection, find-in-document with hit highlighting and next/previous. Virtualised rendering.

**M2** — Scan panel with all `ScanParams` exposed, six built-in presets, custom preset save
and delete, seeded reproducibility, debounced live preview, SSE progress, both `in_place`
and `export` modes, cancel button, and a mandatory irreversible-action confirmation for
`in_place`.

**M3** — Thumbnail grid with drag reorder, multi-select, rotate, delete, duplicate, insert
blank, extract selection to a new document, merge another PDF in, split at a page. Undo and
redo wired to the server for all of it.

**M4** — All annotation kinds from section 7 drawable, selectable, movable, resizable,
deletable, styled (colour, width, opacity). Signature capture by drawing on a pad, typing
with a script font, or uploading a PNG, placed as an image annotation. Annotations survive
save, reload and flatten.

**M5** — Form field list with type-appropriate editors, redaction tool with rectangle and
text-selection modes plus a "search and redact all" action, flatten command, single-span
text replace with warnings surfaced in the UI.

**M6** — All shortcuts, focus management and ARIA on toolbars and dialogs, empty and error
states everywhere, a single `docker compose up` or one PowerShell script that starts both
processes, and the performance targets met.

---

## 15. Notes for the implementer

- Build the op log and the replay engine first. Every other feature is an op; if that
  foundation is wrong, everything after it is rework.
- Write the coordinate conversion helpers and their tests before drawing anything.
- Keep [scan_effect.py](scan_effect.py) around as the reference for the look of the
  `subtle`/`medium`/`heavy` presets.
- Do not add a feature that is not in section 1 of the plan without saying so.
- The scan feature carries a one-line acceptable-use notice in its panel; do not remove it.
