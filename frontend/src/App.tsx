import { useEffect, useMemo, useRef, useState } from 'react';
import { GlobalWorkerOptions, getDocument, type PDFDocumentProxy } from 'pdfjs-dist';

GlobalWorkerOptions.workerSrc = new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url).toString();

type DocumentItem = {
  id: string;
  title: string;
  original_name: string;
  page_count: number;
  version: number;
};

type PageInfo = {
  uuid: string;
  index: number;
  width: number;
  height: number;
  rotation: 0 | 90 | 180 | 270;
  has_text: boolean;
  label: string | null;
};

type SearchHit = {
  page: string;
  preview: string;
};

type OpResult = {
  version: number;
  cursor: number;
};

type Job = {
  id: string;
  status: 'queued' | 'running' | 'done' | 'error' | 'cancelled';
  message: string | null;
};

const defaultScanParams = {
  dpi: 200,
  color_mode: 'gray',
  paper_tint: '#FFFFFF',
  gamma: 1,
  brightness: 1,
  contrast: 1.15,
  jitter: 0.03,
  blur_sigma: 0.35,
  noise_sigma: 8,
  noise_mono: true,
  bw_threshold: 128,
  bw_dither: true,
  jpeg_quality: 75,
  downsample: 1,
};

async function loadDocuments(): Promise<DocumentItem[]> {
  const response = await fetch('/api/documents');
  if (!response.ok) {
    throw new Error('Failed to load documents');
  }
  const payload = (await response.json()) as { items: DocumentItem[] };
  return payload.items;
}

async function loadPages(documentId: string): Promise<PageInfo[]> {
  const response = await fetch(`/api/documents/${documentId}/pages`);
  if (!response.ok) {
    throw new Error('Failed to load pages');
  }
  return (await response.json()) as PageInfo[];
}

export function App() {
  const [docs, setDocs] = useState<DocumentItem[]>([]);
  const [loadingDocs, setLoadingDocs] = useState(false);
  const [selectedDocumentId, setSelectedDocumentId] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [pages, setPages] = useState<PageInfo[]>([]);
  const [zoom, setZoom] = useState(1);
  const [query, setQuery] = useState('');
  const [searchHits, setSearchHits] = useState<SearchHit[]>([]);
  const [activePage, setActivePage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pdfDoc, setPdfDoc] = useState<PDFDocumentProxy | null>(null);
  const [scanPreviewUrl, setScanPreviewUrl] = useState<string | null>(null);
  const [scanStatus, setScanStatus] = useState<string>('');

  const viewerRef = useRef<HTMLDivElement | null>(null);
  const canvasRefs = useRef<Record<string, HTMLCanvasElement | null>>({});

  const selectedDoc = useMemo(
    () => docs.find((doc) => doc.id === selectedDocumentId) ?? null,
    [docs, selectedDocumentId]
  );

  async function refreshDocuments() {
    setLoadingDocs(true);
    setError(null);
    try {
      const next = await loadDocuments();
      setDocs(next);
      if (!selectedDocumentId && next.length > 0) {
        setSelectedDocumentId(next[0].id);
        setVersion(next[0].version);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoadingDocs(false);
    }
  }

  async function refreshPages(docId: string) {
    const next = await loadPages(docId);
    setPages(next);
    if (!activePage && next.length > 0) {
      setActivePage(next[0].uuid);
    }
  }

  useEffect(() => {
    void refreshDocuments();
  }, []);

  useEffect(() => {
    if (!selectedDocumentId) {
      setPages([]);
      return;
    }
    void refreshPages(selectedDocumentId).catch((err) => {
      setError(err instanceof Error ? err.message : 'Failed to load pages');
    });
  }, [selectedDocumentId, version]);

  useEffect(() => {
    if (!selectedDocumentId) {
      setPdfDoc(null);
      return;
    }

    let disposed = false;
    const task = getDocument(`/api/documents/${selectedDocumentId}/file?version=${version}`);
    task.promise
      .then((doc) => {
        if (disposed) {
          void doc.destroy();
          return;
        }
        setPdfDoc(doc);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : 'Failed to load PDF');
      });

    return () => {
      disposed = true;
      task.destroy();
    };
  }, [selectedDocumentId, version]);

  useEffect(() => {
    if (!pdfDoc || pages.length === 0) {
      return;
    }

    let cancelled = false;
    const dpr = window.devicePixelRatio || 1;

    pages.forEach((pageInfo) => {
      const canvas = canvasRefs.current[pageInfo.uuid];
      if (!canvas) {
        return;
      }

      void pdfDoc
        .getPage(pageInfo.index + 1)
        .then((page) => {
          if (cancelled) {
            return;
          }
          const viewport = page.getViewport({ scale: zoom });
          const width = Math.floor(viewport.width * dpr);
          const height = Math.floor(viewport.height * dpr);
          canvas.width = width;
          canvas.height = height;
          canvas.style.width = `${viewport.width}px`;
          canvas.style.height = `${viewport.height}px`;

          const context = canvas.getContext('2d');
          if (!context) {
            return;
          }
          context.setTransform(dpr, 0, 0, dpr, 0, 0);
          void page.render({ canvas, canvasContext: context, viewport }).promise;
        })
        .catch(() => {
          setError('Failed to render page');
        });
    });

    return () => {
      cancelled = true;
    };
  }, [pdfDoc, pages, zoom]);

  function fitWidth() {
    if (!pages.length || !viewerRef.current) {
      return;
    }
    const width = pages[0].width;
    const available = viewerRef.current.clientWidth - 32;
    if (width > 0) {
      setZoom(Math.max(0.4, Math.min(3, available / width)));
    }
  }

  function fitPage() {
    if (!pages.length || !viewerRef.current) {
      return;
    }
    const first = pages[0];
    const widthZoom = (viewerRef.current.clientWidth - 32) / first.width;
    const heightZoom = (window.innerHeight - 220) / first.height;
    setZoom(Math.max(0.4, Math.min(3, Math.min(widthZoom, heightZoom))));
  }

  async function runSearch() {
    if (!selectedDocumentId || !query.trim()) {
      setSearchHits([]);
      return;
    }
    const response = await fetch(`/api/documents/${selectedDocumentId}/search`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ query, caseSensitive: false }),
    });
    if (!response.ok) {
      setError('Search failed');
      return;
    }
    const hits = (await response.json()) as SearchHit[];
    setSearchHits(hits);
  }

  async function uploadFile(file: File) {
    const form = new FormData();
    form.append('file', file);
    const response = await fetch('/api/documents', { method: 'POST', body: form });
    if (!response.ok) {
      setError('Upload failed');
      return;
    }
    const created = (await response.json()) as DocumentItem;
    await refreshDocuments();
    setSelectedDocumentId(created.id);
    setVersion(created.version);
    setActivePage(null);
  }

  async function runRotate() {
    if (!selectedDocumentId || !activePage) {
      return;
    }
    const response = await fetch(`/api/documents/${selectedDocumentId}/ops`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ kind: 'page.rotate', payload: { pages: [activePage], delta: 90 } }),
    });
    if (!response.ok) {
      setError('Rotate failed');
      return;
    }
    const result = (await response.json()) as OpResult;
    setVersion(result.version);
    await refreshDocuments();
  }

  async function runUndo() {
    if (!selectedDocumentId) {
      return;
    }
    const response = await fetch(`/api/documents/${selectedDocumentId}/undo`, { method: 'POST' });
    if (!response.ok) {
      setError('Undo failed');
      return;
    }
    const result = (await response.json()) as OpResult;
    setVersion(result.version);
    await refreshDocuments();
  }

  async function runRedo() {
    if (!selectedDocumentId) {
      return;
    }
    const response = await fetch(`/api/documents/${selectedDocumentId}/redo`, { method: 'POST' });
    if (!response.ok) {
      setError('Redo failed');
      return;
    }
    const result = (await response.json()) as OpResult;
    setVersion(result.version);
    await refreshDocuments();
  }

  async function previewScan() {
    if (!selectedDocumentId || !activePage) {
      return;
    }
    setScanStatus('Building preview...');
    const response = await fetch(`/api/documents/${selectedDocumentId}/scan/preview`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ pageUuid: activePage, previewDpi: 110, params: defaultScanParams }),
    });

    if (!response.ok) {
      setScanStatus('Preview failed');
      return;
    }

    const blob = await response.blob();
    if (scanPreviewUrl) {
      URL.revokeObjectURL(scanPreviewUrl);
    }
    setScanPreviewUrl(URL.createObjectURL(blob));
    setScanStatus('Preview ready');
  }

  async function exportScan() {
    if (!selectedDocumentId) {
      return;
    }
    setScanStatus('Running scan export...');

    const response = await fetch(`/api/documents/${selectedDocumentId}/scan`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ mode: 'export', params: defaultScanParams }),
    });
    if (!response.ok) {
      setScanStatus('Scan export failed');
      return;
    }

    const job = (await response.json()) as Job;
    if (job.status !== 'done') {
      setScanStatus(`Scan status: ${job.status}`);
      return;
    }

    const download = document.createElement('a');
    download.href = `/api/jobs/${job.id}/result`;
    download.download = `scan-${job.id}.pdf`;
    download.click();
    setScanStatus(job.message ?? 'Scan export done');
  }

  return (
    <main className="page">
      <header className="header">
        <h1>myOpenPDF</h1>
        <p>M1+M2 in progress: viewer, op-log-backed versioning, and scan preview/export skeleton.</p>
      </header>

      <section className="toolbar">
        <label className="upload">
          <span>Upload PDF</span>
          <input
            type="file"
            accept="application/pdf"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) {
                void uploadFile(file);
              }
            }}
          />
        </label>

        <button type="button" onClick={() => void refreshDocuments()} disabled={loadingDocs}>
          {loadingDocs ? 'Refreshing...' : 'Refresh Library'}
        </button>
        <button type="button" onClick={fitWidth}>Fit Width</button>
        <button type="button" onClick={fitPage}>Fit Page</button>
        <button type="button" onClick={() => setZoom((z) => Math.max(0.4, z - 0.1))}>-</button>
        <span className="zoom">{Math.round(zoom * 100)}%</span>
        <button type="button" onClick={() => setZoom((z) => Math.min(3, z + 0.1))}>+</button>
        <button type="button" onClick={() => void runRotate()}>Rotate Active</button>
        <button type="button" onClick={() => void runUndo()}>Undo</button>
        <button type="button" onClick={() => void runRedo()}>Redo</button>
      </section>

      {error && <p className="error">{error}</p>}

      <section className="workspace">
        <aside className="sidebar">
          <h2>Library</h2>
          <ul className="doc-list">
            {docs.map((doc) => (
              <li key={doc.id}>
                <button
                  type="button"
                  className={doc.id === selectedDocumentId ? 'doc-btn doc-btn-active' : 'doc-btn'}
                  onClick={() => {
                    setSelectedDocumentId(doc.id);
                    setVersion(doc.version);
                    setActivePage(null);
                    setSearchHits([]);
                  }}
                >
                  <strong>{doc.title}</strong>
                  <span>{doc.page_count} pages</span>
                </button>
              </li>
            ))}
          </ul>

          {selectedDoc && (
            <>
              <h2>Thumbnails</h2>
              <div className="thumbs">
                {pages.map((page) => (
                  <button
                    key={page.uuid}
                    type="button"
                    className={activePage === page.uuid ? 'thumb thumb-active' : 'thumb'}
                    onClick={() => {
                      setActivePage(page.uuid);
                      const target = document.getElementById(`page-${page.uuid}`);
                      target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
                    }}
                  >
                    <img
                      src={`/api/documents/${selectedDoc.id}/pages/${page.uuid}/thumb?dpi=110&version=${version}`}
                      alt={`Page ${page.index + 1}`}
                      loading="lazy"
                    />
                    <span>Page {page.index + 1}</span>
                  </button>
                ))}
              </div>
            </>
          )}
        </aside>

        <section className="viewer-section">
          <div className="search-row">
            <input
              type="text"
              placeholder="Search text"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <button type="button" onClick={() => void runSearch()}>Search</button>
            <span>{searchHits.length} page hit(s)</span>
          </div>

          {!!searchHits.length && (
            <div className="search-hits">
              {searchHits.map((hit) => (
                <button
                  key={`${hit.page}-${hit.preview}`}
                  type="button"
                  onClick={() => {
                    const target = document.getElementById(`page-${hit.page}`);
                    target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
                    setActivePage(hit.page);
                  }}
                >
                  {hit.preview}
                </button>
              ))}
            </div>
          )}

          <div ref={viewerRef} className="viewer">
            {pages.map((pageInfo) => (
              <article
                key={pageInfo.uuid}
                id={`page-${pageInfo.uuid}`}
                className={activePage === pageInfo.uuid ? 'pdf-page pdf-page-active' : 'pdf-page'}
                onClick={() => setActivePage(pageInfo.uuid)}
              >
                <header>Page {pageInfo.index + 1}</header>
                <canvas
                  ref={(element) => {
                    canvasRefs.current[pageInfo.uuid] = element;
                  }}
                />
              </article>
            ))}
          </div>
        </section>

        <aside className="scan-panel">
          <h2>Scan Effect</h2>
          <p className="notice">For archival look-and-feel and testing only. Do not use for forgery.</p>
          <button type="button" onClick={() => void previewScan()} disabled={!activePage}>
            Preview Active Page
          </button>
          <button type="button" onClick={() => void exportScan()} disabled={!selectedDocumentId}>
            Export Scanned PDF
          </button>
          <p>{scanStatus}</p>
          {scanPreviewUrl && <img src={scanPreviewUrl} alt="Scan preview" className="scan-preview" />}
        </aside>
      </section>
    </main>
  );
}
