import { type CSSProperties, type PointerEvent as ReactPointerEvent, type UIEvent, useEffect, useMemo, useRef, useState } from 'react';
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
  progress: number;
  message: string | null;
};

type FormField = {
  page: string;
  name: string;
  field_type: 'text' | 'checkbox' | 'radio' | 'combo' | 'list' | 'signature';
  value: string | boolean | number | null;
  rect: [number, number, number, number] | null;
};

type RectTuple = [number, number, number, number];
type AnnotationKind =
  | 'rect'
  | 'ellipse'
  | 'line'
  | 'highlight'
  | 'underline'
  | 'strikeout'
  | 'note'
  | 'freetext'
  | 'image';

type AnnotationItem = {
  id: string;
  page: string;
  kind: AnnotationKind;
  rect: RectTuple | null;
  text: string | null;
  asset_id: string | null;
};

type AnnotationDragMode = 'move' | 'resize-tl' | 'resize-tr' | 'resize-bl' | 'resize-br';

type AnnotationDragState = {
  annotationId: string;
  pageUuid: string;
  pointerId: number;
  mode: AnnotationDragMode;
  startRect: RectTuple;
  startClientX: number;
  startClientY: number;
  pageWidth: number;
  pageHeight: number;
};

type CompressionEstimate = {
  sourceBytes: number;
  estimatedBytes: number;
  estimatedReductionPercent: number;
  profile: CompressionProfile;
  note: string | null;
};

type CompressionProfile = 'light' | 'balanced' | 'strong';
type ExportFormat = 'pdf' | 'png' | 'jpeg';
type ExportTarget = 'all' | 'active';

const VIEWER_BUFFER_PX = 1200;
const THUMB_CHUNK_SIZE = 60;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function buildAnnotationPayload(item: AnnotationItem, rect: RectTuple | null): Record<string, unknown> {
  return {
    id: item.id,
    page: item.page,
    kind: item.kind,
    rect,
    text: item.text,
    opacity: 0.95,
    width: item.kind === 'line' ? 2.2 : 1.6,
    color: [0.08, 0.33, 0.75],
    fill: item.kind === 'rect' || item.kind === 'ellipse' ? [0.76, 0.86, 0.99] : undefined,
    asset_id: item.asset_id,
  };
}

async function waitForJob(
  jobId: string,
  onProgress: (message: string) => void,
  timeoutMs = 120000
): Promise<Job> {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const response = await fetch(`/api/jobs/${jobId}`);
    if (!response.ok) {
      throw new Error('Failed to read job state');
    }
    const job = (await response.json()) as Job;
    const progressPercent = Math.round((job.progress ?? 0) * 100);
    onProgress(`${job.status} ${progressPercent}%${job.message ? ` - ${job.message}` : ''}`);
    if (job.status === 'done' || job.status === 'error' || job.status === 'cancelled') {
      return job;
    }
    await sleep(180);
  }
  throw new Error('Job timed out');
}

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

async function loadDocument(documentId: string): Promise<DocumentItem> {
  const response = await fetch(`/api/documents/${documentId}`);
  if (!response.ok) {
    throw new Error('Failed to load document');
  }
  return (await response.json()) as DocumentItem;
}

async function loadForms(documentId: string): Promise<FormField[]> {
  const response = await fetch(`/api/documents/${documentId}/forms`);
  if (!response.ok) {
    throw new Error('Failed to load forms');
  }
  return (await response.json()) as FormField[];
}

async function loadAnnotations(documentId: string): Promise<AnnotationItem[]> {
  const response = await fetch(`/api/documents/${documentId}/annotations`);
  if (!response.ok) {
    throw new Error('Failed to load annotations');
  }
  return (await response.json()) as AnnotationItem[];
}

function makeUuid(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
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
  const [opStatus, setOpStatus] = useState<string>('');
  const [forms, setForms] = useState<FormField[]>([]);
  const [annotationKind, setAnnotationKind] = useState<AnnotationKind>('highlight');
  const [annotationText, setAnnotationText] = useState('Note');
  const [annotationStatus, setAnnotationStatus] = useState<string>('');
  const [annotations, setAnnotations] = useState<AnnotationItem[]>([]);
  const [selectedAnnotationId, setSelectedAnnotationId] = useState<string | null>(null);
  const [draggingAnnotationId, setDraggingAnnotationId] = useState<string | null>(null);
  const [annotationAssetId, setAnnotationAssetId] = useState<string | null>(null);
  const [exportStatus, setExportStatus] = useState<string>('');
  const [exportFormat, setExportFormat] = useState<ExportFormat>('pdf');
  const [exportTarget, setExportTarget] = useState<ExportTarget>('all');
  const [exportDpi, setExportDpi] = useState(200);
  const [exportFlatten, setExportFlatten] = useState(false);
  const [compressStatus, setCompressStatus] = useState<string>('');
  const [compressProfile, setCompressProfile] = useState<CompressionProfile>('balanced');
  const [compressDpi, setCompressDpi] = useState(200);
  const [compressStripMetadata, setCompressStripMetadata] = useState(true);
  const [compressEstimate, setCompressEstimate] = useState<CompressionEstimate | null>(null);
  const [currentJobId, setCurrentJobId] = useState<string | null>(null);
  const [thumbVisibleCount, setThumbVisibleCount] = useState(THUMB_CHUNK_SIZE);
  const [viewerScrollTop, setViewerScrollTop] = useState(0);
  const [viewerViewportHeight, setViewerViewportHeight] = useState(0);

  const viewerRef = useRef<HTMLDivElement | null>(null);
  const canvasRefs = useRef<Record<string, HTMLCanvasElement | null>>({});
  const renderCycleRef = useRef(0);
  const renderTaskRefs = useRef<Record<string, { cancel: () => void }>>({});
  const dragStateRef = useRef<AnnotationDragState | null>(null);
  const annotationsRef = useRef<AnnotationItem[]>([]);

  const selectedDoc = useMemo(
    () => docs.find((doc) => doc.id === selectedDocumentId) ?? null,
    [docs, selectedDocumentId]
  );

  const activePageAnnotations = useMemo(
    () => annotations.filter((item) => item.page === activePage),
    [annotations, activePage]
  );

  const selectedAnnotation = useMemo(
    () => annotations.find((item) => item.id === selectedAnnotationId) ?? null,
    [annotations, selectedAnnotationId]
  );

  const viewerMetrics = useMemo(() => {
    const itemHeights = pages.map((page) => {
      const rotated = page.rotation === 90 || page.rotation === 270;
      const pageHeight = rotated ? page.width : page.height;
      return Math.max(160, pageHeight * zoom + 52);
    });

    const offsets: number[] = [0];
    itemHeights.forEach((height) => {
      offsets.push(offsets[offsets.length - 1] + height);
    });

    return {
      itemHeights,
      offsets,
      totalHeight: offsets[offsets.length - 1] ?? 0,
    };
  }, [pages, zoom]);

  const visibleRange = useMemo(() => {
    if (pages.length === 0) {
      return { start: 0, end: -1 };
    }

    const targetStart = Math.max(0, viewerScrollTop - VIEWER_BUFFER_PX);
    const targetEnd = viewerScrollTop + Math.max(viewerViewportHeight, 1) + VIEWER_BUFFER_PX;

    let start = 0;
    while (start < pages.length && viewerMetrics.offsets[start + 1] < targetStart) {
      start += 1;
    }

    let end = start;
    while (end < pages.length - 1 && viewerMetrics.offsets[end] < targetEnd) {
      end += 1;
    }

    return { start, end: Math.max(start, end) };
  }, [pages.length, viewerMetrics.offsets, viewerScrollTop, viewerViewportHeight]);

  const visiblePages = useMemo(() => {
    if (visibleRange.end < visibleRange.start) {
      return [];
    }
    return pages.slice(visibleRange.start, visibleRange.end + 1);
  }, [pages, visibleRange.end, visibleRange.start]);

  const topSpacerHeight = visibleRange.start > 0 ? viewerMetrics.offsets[visibleRange.start] : 0;
  const bottomSpacerHeight =
    visibleRange.end >= visibleRange.start
      ? Math.max(0, viewerMetrics.totalHeight - viewerMetrics.offsets[visibleRange.end + 1])
      : 0;

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
    const activeStillExists = activePage ? next.some((page) => page.uuid === activePage) : false;
    if ((!activePage || !activeStillExists) && next.length > 0) {
      setActivePage(next[0].uuid);
    }
    if (next.length === 0) {
      setActivePage(null);
    }
  }

  async function refreshSelectedVersion(docId: string) {
    const row = await loadDocument(docId);
    setVersion(row.version);
  }

  async function refreshForms(docId: string) {
    const next = await loadForms(docId);
    setForms(next);
  }

  async function refreshAnnotations(docId: string) {
    const next = await loadAnnotations(docId);
    setAnnotations(next);
    if (next.length === 0) {
      setSelectedAnnotationId(null);
      return;
    }
    if (selectedAnnotationId && next.some((item) => item.id === selectedAnnotationId)) {
      return;
    }
    setSelectedAnnotationId(next[0].id);
  }

  useEffect(() => {
    void refreshDocuments();
  }, []);

  useEffect(() => {
    const refreshViewport = () => {
      setViewerViewportHeight(viewerRef.current?.clientHeight ?? 0);
    };
    refreshViewport();
    window.addEventListener('resize', refreshViewport);
    return () => {
      window.removeEventListener('resize', refreshViewport);
    };
  }, [pages.length, zoom]);

  useEffect(() => {
    setThumbVisibleCount(Math.min(THUMB_CHUNK_SIZE, pages.length));
  }, [pages.length, selectedDocumentId]);

  useEffect(() => {
    setAnnotations([]);
    setSelectedAnnotationId(null);
    setDraggingAnnotationId(null);
    dragStateRef.current = null;
    setAnnotationAssetId(null);
    setAnnotationStatus('');
  }, [selectedDocumentId]);

  useEffect(() => {
    annotationsRef.current = annotations;
  }, [annotations]);

  useEffect(() => {
    if (!selectedDocumentId) {
      setPages([]);
      setForms([]);
      setAnnotations([]);
      return;
    }

    void refreshPages(selectedDocumentId).catch((err) => {
      setError(err instanceof Error ? err.message : 'Failed to load pages');
    });

    void refreshForms(selectedDocumentId).catch(() => {
      setForms([]);
    });

    void refreshAnnotations(selectedDocumentId).catch(() => {
      setAnnotations([]);
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
    if (!pdfDoc || visiblePages.length === 0) {
      return;
    }

    renderCycleRef.current += 1;
    const cycle = renderCycleRef.current;
    const dpr = window.devicePixelRatio || 1;
    const queue = [...visiblePages];

    Object.values(renderTaskRefs.current).forEach((task) => {
      task.cancel();
    });
    renderTaskRefs.current = {};

    const workerCount = Math.min(3, Math.max(1, Math.floor((navigator.hardwareConcurrency || 4) / 2)));

    const runWorker = async () => {
      while (queue.length > 0 && cycle === renderCycleRef.current) {
        const pageInfo = queue.shift();
        if (!pageInfo) {
          return;
        }
        const canvas = canvasRefs.current[pageInfo.uuid];
        if (!canvas) {
          continue;
        }

        try {
          const page = await pdfDoc.getPage(pageInfo.index + 1);
          if (cycle !== renderCycleRef.current) {
            return;
          }

          const viewport = page.getViewport({ scale: zoom });
          canvas.width = Math.floor(viewport.width * dpr);
          canvas.height = Math.floor(viewport.height * dpr);
          canvas.style.width = `${viewport.width}px`;
          canvas.style.height = `${viewport.height}px`;

          const context = canvas.getContext('2d');
          if (!context) {
            continue;
          }
          context.setTransform(dpr, 0, 0, dpr, 0, 0);

          const renderTask = page.render({ canvas, canvasContext: context, viewport });
          renderTaskRefs.current[pageInfo.uuid] = { cancel: () => renderTask.cancel() };
          await renderTask.promise.catch(() => undefined);
          delete renderTaskRefs.current[pageInfo.uuid];
        } catch {
          if (cycle === renderCycleRef.current) {
            setError('Failed to render page');
          }
        }
      }
    };

    Array.from({ length: workerCount }, () => runWorker());

    return () => {
      renderCycleRef.current += 1;
      Object.values(renderTaskRefs.current).forEach((task) => {
        task.cancel();
      });
      renderTaskRefs.current = {};
    };
  }, [pdfDoc, visiblePages, zoom]);

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

  function scrollToPage(pageUuid: string) {
    const index = pages.findIndex((page) => page.uuid === pageUuid);
    if (index < 0 || !viewerRef.current) {
      return;
    }
    setActivePage(pageUuid);
    viewerRef.current.scrollTo({
      top: viewerMetrics.offsets[index],
      behavior: 'smooth',
    });
  }

  function onViewerScroll(event: UIEvent<HTMLDivElement>) {
    const scrollTop = event.currentTarget.scrollTop;
    const viewportHeight = event.currentTarget.clientHeight;
    setViewerScrollTop(scrollTop);
    setViewerViewportHeight(viewportHeight);

    if (!pages.length) {
      return;
    }

    const center = scrollTop + viewportHeight / 2;
    let index = 0;
    while (index < pages.length - 1 && viewerMetrics.offsets[index + 1] < center) {
      index += 1;
    }
    const page = pages[index];
    if (page && page.uuid !== activePage) {
      setActivePage(page.uuid);
    }
  }

  function onThumbsScroll(event: UIEvent<HTMLDivElement>) {
    const element = event.currentTarget;
    const distanceToBottom = element.scrollHeight - (element.scrollTop + element.clientHeight);
    if (distanceToBottom < 120) {
      setThumbVisibleCount((current) => Math.min(pages.length, current + THUMB_CHUNK_SIZE));
    }
  }

  function computeDraggedRect(drag: AnnotationDragState, clientX: number, clientY: number): RectTuple {
    const minSize = 12;
    const dx = (clientX - drag.startClientX) / Math.max(zoom, 0.01);
    const dy = (clientY - drag.startClientY) / Math.max(zoom, 0.01);

    if (drag.mode === 'move') {
      const width = drag.startRect[2] - drag.startRect[0];
      const height = drag.startRect[3] - drag.startRect[1];
      const x0 = Math.max(0, Math.min(drag.pageWidth - width, drag.startRect[0] + dx));
      const y0 = Math.max(0, Math.min(drag.pageHeight - height, drag.startRect[1] + dy));
      return [x0, y0, x0 + width, y0 + height];
    }

    let [x0, y0, x1, y1] = drag.startRect;
    if (drag.mode === 'resize-tl') {
      x0 += dx;
      y0 += dy;
    }
    if (drag.mode === 'resize-tr') {
      x1 += dx;
      y0 += dy;
    }
    if (drag.mode === 'resize-bl') {
      x0 += dx;
      y1 += dy;
    }
    if (drag.mode === 'resize-br') {
      x1 += dx;
      y1 += dy;
    }

    const resizeFromLeft = drag.mode === 'resize-tl' || drag.mode === 'resize-bl';
    const resizeFromTop = drag.mode === 'resize-tl' || drag.mode === 'resize-tr';

    let nextX0 = Math.min(x0, x1);
    let nextX1 = Math.max(x0, x1);
    let nextY0 = Math.min(y0, y1);
    let nextY1 = Math.max(y0, y1);

    if (nextX1 - nextX0 < minSize) {
      if (resizeFromLeft) {
        nextX0 = nextX1 - minSize;
      } else {
        nextX1 = nextX0 + minSize;
      }
    }
    if (nextY1 - nextY0 < minSize) {
      if (resizeFromTop) {
        nextY0 = nextY1 - minSize;
      } else {
        nextY1 = nextY0 + minSize;
      }
    }

    if (nextX0 < 0) {
      nextX1 -= nextX0;
      nextX0 = 0;
    }
    if (nextY0 < 0) {
      nextY1 -= nextY0;
      nextY0 = 0;
    }
    if (nextX1 > drag.pageWidth) {
      const overflow = nextX1 - drag.pageWidth;
      nextX0 -= overflow;
      nextX1 = drag.pageWidth;
    }
    if (nextY1 > drag.pageHeight) {
      const overflow = nextY1 - drag.pageHeight;
      nextY0 -= overflow;
      nextY1 = drag.pageHeight;
    }

    nextX0 = Math.max(0, Math.min(nextX0, drag.pageWidth - minSize));
    nextY0 = Math.max(0, Math.min(nextY0, drag.pageHeight - minSize));
    nextX1 = Math.max(nextX0 + minSize, Math.min(drag.pageWidth, nextX1));
    nextY1 = Math.max(nextY0 + minSize, Math.min(drag.pageHeight, nextY1));
    return [nextX0, nextY0, nextX1, nextY1];
  }

  async function persistAnnotationRect(annotationId: string, startRect: RectTuple, nextRect: RectTuple) {
    const delta =
      Math.abs(startRect[0] - nextRect[0]) +
      Math.abs(startRect[1] - nextRect[1]) +
      Math.abs(startRect[2] - nextRect[2]) +
      Math.abs(startRect[3] - nextRect[3]);
    if (delta < 0.4) {
      return;
    }

    const item = annotationsRef.current.find((entry) => entry.id === annotationId);
    if (!item) {
      return;
    }

    const ok = await applyOp('annot.update', {
      annot: buildAnnotationPayload(item, nextRect),
    });
    if (!ok) {
      setAnnotationStatus('Update annotation failed');
      return;
    }
    setAnnotationStatus('Updated selected annotation geometry');
  }

  function beginAnnotationDrag(
    event: ReactPointerEvent<HTMLElement>,
    annotation: AnnotationItem,
    mode: AnnotationDragMode
  ) {
    if (!annotation.rect) {
      return;
    }
    const page = pages.find((entry) => entry.uuid === annotation.page);
    if (!page) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();

    dragStateRef.current = {
      annotationId: annotation.id,
      pageUuid: annotation.page,
      pointerId: event.pointerId,
      mode,
      startRect: [...annotation.rect] as RectTuple,
      startClientX: event.clientX,
      startClientY: event.clientY,
      pageWidth: page.width,
      pageHeight: page.height,
    };

    setSelectedAnnotationId(annotation.id);
    setActivePage(annotation.page);
    setDraggingAnnotationId(annotation.id);
  }

  useEffect(() => {
    const onPointerMove = (event: PointerEvent) => {
      const drag = dragStateRef.current;
      if (!drag || event.pointerId !== drag.pointerId) {
        return;
      }

      event.preventDefault();
      const nextRect = computeDraggedRect(drag, event.clientX, event.clientY);
      setAnnotations((current) =>
        current.map((item) =>
          item.id === drag.annotationId
            ? {
                ...item,
                rect: nextRect,
              }
            : item
        )
      );
    };

    const onPointerFinish = (event: PointerEvent) => {
      const drag = dragStateRef.current;
      if (!drag || event.pointerId !== drag.pointerId) {
        return;
      }

      const nextRect = computeDraggedRect(drag, event.clientX, event.clientY);
      setAnnotations((current) =>
        current.map((item) =>
          item.id === drag.annotationId
            ? {
                ...item,
                rect: nextRect,
              }
            : item
        )
      );

      dragStateRef.current = null;
      setDraggingAnnotationId(null);
      void persistAnnotationRect(drag.annotationId, drag.startRect, nextRect);
    };

    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerFinish);
    window.addEventListener('pointercancel', onPointerFinish);
    return () => {
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerFinish);
      window.removeEventListener('pointercancel', onPointerFinish);
    };
  }, [pages, zoom]);

  function buildAnnotationRect(page: PageInfo, kind: AnnotationKind): RectTuple {
    if (kind === 'line') {
      const y = page.height * 0.45;
      return [page.width * 0.25, y, page.width * 0.75, y + 10];
    }
    if (kind === 'highlight' || kind === 'underline' || kind === 'strikeout') {
      const y = page.height * 0.35;
      return [page.width * 0.2, y, page.width * 0.75, y + 24];
    }
    const width = Math.max(140, Math.min(240, page.width * 0.35));
    const height = kind === 'note' ? 80 : 96;
    const x = (page.width - width) / 2;
    const y = (page.height - height) / 2;
    return [x, y, x + width, y + height];
  }

  async function runAddAnnotation() {
    if (!activePage) {
      return;
    }

    const page = pages.find((item) => item.uuid === activePage);
    if (!page) {
      return;
    }

    if (annotationKind === 'image' && !annotationAssetId) {
      setAnnotationStatus('Upload a stamp image first.');
      return;
    }

    const item: AnnotationItem = {
      id: makeUuid(),
      page: activePage,
      kind: annotationKind,
      rect: buildAnnotationRect(page, annotationKind),
      text: annotationText,
      asset_id: annotationAssetId,
    };

    const ok = await applyOp('annot.add', { annot: buildAnnotationPayload(item, item.rect) });
    if (!ok) {
      setAnnotationStatus('Add annotation failed');
      return;
    }

    setSelectedAnnotationId(item.id);
    setAnnotationStatus(`Added ${annotationKind} annotation`);
  }

  async function runNudgeLastAnnotation() {
    if (!activePage) {
      return;
    }
    const selected = annotations.find((item) => item.id === selectedAnnotationId && item.page === activePage);
    if (!selected) {
      setAnnotationStatus('Select an annotation on the active page first');
      return;
    }
    if (!selected.rect) {
      setAnnotationStatus('Selected annotation has no editable rectangle');
      return;
    }

    const moved: AnnotationItem = {
      ...selected,
      rect: [selected.rect[0] + 12, selected.rect[1] + 10, selected.rect[2] + 12, selected.rect[3] + 10],
    };

    const ok = await applyOp('annot.update', {
      annot: buildAnnotationPayload(moved, moved.rect),
    });

    if (!ok) {
      setAnnotationStatus('Update annotation failed');
      return;
    }

    setAnnotationStatus('Moved selected annotation');
  }

  async function runDeleteLastAnnotation() {
    if (!activePage) {
      return;
    }
    const selected = annotations.find((item) => item.id === selectedAnnotationId && item.page === activePage);
    if (!selected) {
      setAnnotationStatus('Select an annotation on the active page first');
      return;
    }

    const ok = await applyOp('annot.delete', { id: selected.id });
    if (!ok) {
      setAnnotationStatus('Delete annotation failed');
      return;
    }

    setSelectedAnnotationId(null);
    setAnnotationStatus('Deleted selected annotation');
  }

  async function uploadStampImage(file: File) {
    if (!selectedDocumentId) {
      return;
    }
    const form = new FormData();
    form.append('file', file);

    const response = await fetch(`/api/documents/${selectedDocumentId}/assets`, {
      method: 'POST',
      body: form,
    });
    if (!response.ok) {
      setAnnotationStatus('Stamp upload failed');
      return;
    }

    const payload = (await response.json()) as { assetId: string; mimeType: string };
    if (!payload.mimeType.startsWith('image/')) {
      setAnnotationStatus('Uploaded asset is not an image');
      return;
    }

    setAnnotationAssetId(payload.assetId);
    setAnnotationKind('image');
    setAnnotationStatus('Stamp image ready');
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

  async function cancelCurrentJob() {
    if (!currentJobId) {
      return;
    }
    await fetch(`/api/jobs/${currentJobId}/cancel`, { method: 'POST' });
    setScanStatus((value) => (value ? `${value} (cancel requested)` : value));
    setExportStatus((value) => (value ? `${value} (cancel requested)` : value));
    setCompressStatus((value) => (value ? `${value} (cancel requested)` : value));
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

  async function applyOp(kind: string, payload: Record<string, unknown>) {
    if (!selectedDocumentId) {
      return false;
    }

    const response = await fetch(`/api/documents/${selectedDocumentId}/ops`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ kind, payload }),
    });
    if (!response.ok) {
      setOpStatus(`${kind} failed`);
      return false;
    }

    const result = (await response.json()) as OpResult;
    setVersion(result.version);
    await refreshDocuments();
    if (selectedDocumentId) {
      await refreshForms(selectedDocumentId).catch(() => {
        setForms([]);
      });
      await refreshAnnotations(selectedDocumentId).catch(() => {
        setAnnotations([]);
      });
    }
    return true;
  }

  async function runRotate() {
    if (activePage) {
      const ok = await applyOp('page.rotate', { pages: [activePage], delta: 90 });
      if (ok) {
        setOpStatus('Rotated active page');
      }
    }
  }

  async function runDeleteActivePage() {
    if (activePage) {
      const ok = await applyOp('page.delete', { pages: [activePage] });
      if (ok) {
        setOpStatus('Deleted active page');
      }
    }
  }

  async function runDuplicateActivePage() {
    if (activePage) {
      const ok = await applyOp('page.duplicate', {
        page: activePage,
        newUuid: makeUuid(),
        after: activePage,
      });
      if (ok) {
        setOpStatus('Duplicated active page');
      }
    }
  }

  async function runInsertBlankPage() {
    const ok = await applyOp('page.insert_blank', {
      newUuid: makeUuid(),
      after: activePage,
      width: 595,
      height: 842,
    });
    if (ok) {
      setOpStatus('Inserted blank page');
    }
  }

  async function runImportPdf(file: File) {
    if (!selectedDocumentId) {
      return;
    }

    const form = new FormData();
    form.append('file', file);

    const upload = await fetch(`/api/documents/${selectedDocumentId}/assets`, {
      method: 'POST',
      body: form,
    });
    if (!upload.ok) {
      setOpStatus('Import upload failed');
      return;
    }

    const asset = (await upload.json()) as { assetId: string; pageCount: number | null };
    const count = Math.max(1, asset.pageCount ?? 1);
    const newUuids = Array.from({ length: count }, () => makeUuid());

    const ok = await applyOp('page.import', {
      assetId: asset.assetId,
      pages: null,
      after: activePage,
      newUuids,
    });
    if (ok) {
      setOpStatus('Merged imported PDF pages');
    }
  }

  async function runFlatten() {
    const ok = await applyOp('doc.flatten', { annots: true, widgets: true });
    if (ok) {
      setOpStatus('Flattened annotations/forms');
    }
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
    setOpStatus('Undo applied');
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
    setOpStatus('Redo applied');
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

    const created = (await response.json()) as Job;
    setCurrentJobId(created.id);
    try {
      const job = await waitForJob(created.id, (message) => setScanStatus(`Scan ${message}`));
      if (job.status !== 'done') {
        setScanStatus(job.message ?? `Scan status: ${job.status}`);
        return;
      }

      const download = document.createElement('a');
      download.href = `/api/jobs/${job.id}/result`;
      download.download = `scan-${job.id}.pdf`;
      download.click();
      setScanStatus(job.message ?? 'Scan export done');
    } catch {
      setScanStatus('Scan export failed');
    } finally {
      setCurrentJobId(null);
    }
  }

  async function applyScanInPlace() {
    if (!selectedDocumentId) {
      return;
    }

    const confirmed = window.confirm(
      'In-place scan will make this branch image-only and disable undo/redo. Continue?'
    );
    if (!confirmed) {
      return;
    }

    setScanStatus('Applying scan in place...');
    const response = await fetch(`/api/documents/${selectedDocumentId}/scan`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ mode: 'in_place', params: defaultScanParams }),
    });
    if (!response.ok) {
      setScanStatus('In-place scan failed');
      return;
    }

    const job = (await response.json()) as Job;
    if (job.status === 'done') {
      await refreshSelectedVersion(selectedDocumentId);
      await refreshDocuments();
    }
    setScanStatus(job.message ?? `Scan status: ${job.status}`);
  }

  async function runExtractActivePage() {
    if (!selectedDocumentId || !activePage) {
      return;
    }

    const response = await fetch(`/api/documents/${selectedDocumentId}/extract`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ pages: [activePage], title: 'Extracted page' }),
    });
    if (!response.ok) {
      setOpStatus('Extract failed');
      return;
    }

    const created = (await response.json()) as DocumentItem;
    await refreshDocuments();
    setSelectedDocumentId(created.id);
    setVersion(created.version);
    setActivePage(null);
    setOpStatus('Extracted active page to a new document');
  }

  async function runSplitAfterActivePage() {
    if (!selectedDocumentId || !activePage) {
      return;
    }

    const activeIndex = pages.findIndex((page) => page.uuid === activePage);
    if (activeIndex < 0 || activeIndex >= pages.length - 1) {
      setOpStatus('Split needs an active page that is not the last one');
      return;
    }

    const response = await fetch(`/api/documents/${selectedDocumentId}/split`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ splitAfterIndex: activeIndex }),
    });
    if (!response.ok) {
      setOpStatus('Split failed');
      return;
    }

    await refreshDocuments();
    setOpStatus('Split document into two new files');
  }

  async function runExport() {
    if (!selectedDocumentId) {
      return;
    }

    const pagesPayload = exportTarget === 'active' && activePage ? [activePage] : null;
    setExportStatus('Preparing export...');

    const response = await fetch(`/api/documents/${selectedDocumentId}/export`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        format: exportFormat,
        flatten: exportFlatten,
        pages: pagesPayload,
        dpi: exportDpi,
      }),
    });
    if (!response.ok) {
      setExportStatus('Export failed');
      return;
    }

    const created = (await response.json()) as Job;
    setCurrentJobId(created.id);
    try {
      const job = await waitForJob(created.id, (message) => setExportStatus(`Export ${message}`));
      if (job.status !== 'done') {
        setExportStatus(job.message ?? `Export status: ${job.status}`);
        return;
      }

      const selectedCount = pagesPayload ? pagesPayload.length : pages.length;
      const extension =
        exportFormat === 'pdf'
          ? 'pdf'
          : selectedCount > 1
            ? 'zip'
            : exportFormat === 'jpeg'
              ? 'jpg'
              : 'png';
      const download = document.createElement('a');
      download.href = `/api/jobs/${job.id}/result`;
      download.download = `export-${job.id}.${extension}`;
      download.click();
      setExportStatus(job.message ?? 'Export done');
    } catch {
      setExportStatus('Export failed');
    } finally {
      setCurrentJobId(null);
    }
  }

  async function runCompress() {
    if (!selectedDocumentId) {
      return;
    }

    setCompressStatus('Running compression...');
    const response = await fetch(`/api/documents/${selectedDocumentId}/compress`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        profile: compressProfile,
        stripMetadata: compressStripMetadata,
        imageDpi: compressDpi,
      }),
    });

    if (!response.ok) {
      setCompressStatus('Compression failed');
      return;
    }

    const created = (await response.json()) as Job;
    setCurrentJobId(created.id);
    try {
      const job = await waitForJob(created.id, (message) => setCompressStatus(`Compression ${message}`));
      if (job.status !== 'done') {
        setCompressStatus(job.message ?? `Compression status: ${job.status}`);
        return;
      }

      const download = document.createElement('a');
      download.href = `/api/jobs/${job.id}/result`;
      download.download = `compressed-${job.id}.pdf`;
      download.click();
      setCompressStatus(job.message ?? 'Compression done');
    } catch {
      setCompressStatus('Compression failed');
    } finally {
      setCurrentJobId(null);
    }
  }

  async function estimateCompress() {
    if (!selectedDocumentId) {
      return;
    }

    setCompressStatus('Estimating output size...');
    const response = await fetch(`/api/documents/${selectedDocumentId}/compress/estimate`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        profile: compressProfile,
        stripMetadata: compressStripMetadata,
        imageDpi: compressDpi,
      }),
    });

    if (!response.ok) {
      setCompressStatus('Estimate failed');
      setCompressEstimate(null);
      return;
    }

    const estimate = (await response.json()) as CompressionEstimate;
    setCompressEstimate(estimate);
    setCompressStatus('Estimate ready');
  }

  function annotationLabel(item: AnnotationItem): string {
    if (item.kind === 'freetext' || item.kind === 'note') {
      return item.text?.slice(0, 32) || item.kind;
    }
    return item.kind;
  }

  function annotationRectStyle(item: AnnotationItem): CSSProperties {
    if (!item.rect) {
      return { display: 'none' };
    }
    const [x0, y0, x1, y1] = item.rect;
    return {
      left: `${x0 * zoom}px`,
      top: `${y0 * zoom}px`,
      width: `${Math.max(2, (x1 - x0) * zoom)}px`,
      height: `${Math.max(2, (y1 - y0) * zoom)}px`,
    };
  }

  function formatBytes(size: number): string {
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = size;
    let unitIndex = 0;
    while (value >= 1024 && unitIndex < units.length - 1) {
      value /= 1024;
      unitIndex += 1;
    }
    if (unitIndex === 0) {
      return `${Math.floor(value)} ${units[unitIndex]}`;
    }
    return `${value.toFixed(1)} ${units[unitIndex]}`;
  }

  return (
    <main className="page">
      <header className="header">
        <h1>myOpenPDF</h1>
        <p>M1 to M6 foundation: viewer, page manager, scan effects, flatten, extract/split, and export/compress flows.</p>
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
        <button type="button" onClick={() => void runDuplicateActivePage()} disabled={!activePage}>Duplicate</button>
        <button type="button" onClick={() => void runDeleteActivePage()} disabled={!activePage}>Delete</button>
        <button type="button" onClick={() => void runInsertBlankPage()}>Insert Blank</button>
        <button type="button" onClick={() => void runExtractActivePage()} disabled={!activePage}>Extract Active</button>
        <button
          type="button"
          onClick={() => void runSplitAfterActivePage()}
          disabled={!activePage || pages.length < 2}
        >
          Split After Active
        </button>
        <button type="button" onClick={() => void runFlatten()} disabled={!selectedDocumentId}>Flatten</button>
        <button type="button" onClick={() => void runUndo()}>Undo</button>
        <button type="button" onClick={() => void runRedo()}>Redo</button>
        <button type="button" onClick={() => void cancelCurrentJob()} disabled={!currentJobId}>
          Cancel Running Job
        </button>
        <label className="upload upload-secondary">
          <span>Merge PDF</span>
          <input
            type="file"
            accept="application/pdf"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) {
                void runImportPdf(file);
                event.currentTarget.value = '';
              }
            }}
          />
        </label>
      </section>

      {error && <p className="error">{error}</p>}
      {opStatus && <p>{opStatus}</p>}

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
              <div className="thumbs" onScroll={onThumbsScroll}>
                {pages.slice(0, thumbVisibleCount).map((page) => (
                  <button
                    key={page.uuid}
                    type="button"
                    className={activePage === page.uuid ? 'thumb thumb-active' : 'thumb'}
                    onClick={() => {
                      scrollToPage(page.uuid);
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
              {thumbVisibleCount < pages.length && (
                <p className="muted-small">
                  Loaded {thumbVisibleCount} of {pages.length} thumbnails. Scroll to load more.
                </p>
              )}
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
                  onClick={() => scrollToPage(hit.page)}
                >
                  {hit.preview}
                </button>
              ))}
            </div>
          )}

          <div
            ref={viewerRef}
            className="viewer"
            onScroll={onViewerScroll}
          >
            {topSpacerHeight > 0 && <div style={{ height: `${topSpacerHeight}px` }} />}
            {visiblePages.map((pageInfo) => (
              <article
                key={pageInfo.uuid}
                id={`page-${pageInfo.uuid}`}
                className={activePage === pageInfo.uuid ? 'pdf-page pdf-page-active' : 'pdf-page'}
                onClick={() => setActivePage(pageInfo.uuid)}
              >
                <header>Page {pageInfo.index + 1}</header>
                <div className="page-canvas-wrap">
                  <canvas
                    ref={(element) => {
                      canvasRefs.current[pageInfo.uuid] = element;
                    }}
                  />
                  <div className="annotation-layer">
                    {annotations
                      .filter((annotation) => annotation.page === pageInfo.uuid)
                      .map((annotation) => (
                        <button
                          key={annotation.id}
                          type="button"
                          className={
                            selectedAnnotationId === annotation.id
                              ? draggingAnnotationId === annotation.id
                                ? 'annotation-box annotation-box-selected annotation-box-dragging'
                                : 'annotation-box annotation-box-selected'
                              : 'annotation-box'
                          }
                          style={annotationRectStyle(annotation)}
                          onPointerDown={(event) => beginAnnotationDrag(event, annotation, 'move')}
                          onClick={(event) => {
                            event.stopPropagation();
                            setSelectedAnnotationId(annotation.id);
                            setActivePage(pageInfo.uuid);
                          }}
                          title={annotationLabel(annotation)}
                          aria-label={`Annotation ${annotationLabel(annotation)}`}
                        >
                          {selectedAnnotationId === annotation.id && (
                            <>
                              <span
                                className="annotation-handle tl"
                                onPointerDown={(event) => beginAnnotationDrag(event, annotation, 'resize-tl')}
                              />
                              <span
                                className="annotation-handle tr"
                                onPointerDown={(event) => beginAnnotationDrag(event, annotation, 'resize-tr')}
                              />
                              <span
                                className="annotation-handle bl"
                                onPointerDown={(event) => beginAnnotationDrag(event, annotation, 'resize-bl')}
                              />
                              <span
                                className="annotation-handle br"
                                onPointerDown={(event) => beginAnnotationDrag(event, annotation, 'resize-br')}
                              />
                            </>
                          )}
                        </button>
                      ))}
                  </div>
                </div>
              </article>
            ))}
            {bottomSpacerHeight > 0 && <div style={{ height: `${bottomSpacerHeight}px` }} />}
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
          <button type="button" onClick={() => void applyScanInPlace()} disabled={!selectedDocumentId}>
            Apply Scan In Place
          </button>
          <p>{scanStatus}</p>
          {scanPreviewUrl && <img src={scanPreviewUrl} alt="Scan preview" className="scan-preview" />}

          <h2>Forms</h2>
          <p>{forms.length} form field(s) detected in current version.</p>

          <h2>Annotations</h2>
          <label className="field">
            <span>Tool</span>
            <select
              value={annotationKind}
              onChange={(event) => setAnnotationKind(event.target.value as AnnotationKind)}
            >
              <option value="highlight">Highlight</option>
              <option value="underline">Underline</option>
              <option value="strikeout">Strikeout</option>
              <option value="rect">Rectangle</option>
              <option value="ellipse">Ellipse</option>
              <option value="line">Line</option>
              <option value="note">Sticky note</option>
              <option value="freetext">Text box</option>
              <option value="image">Image stamp</option>
            </select>
          </label>

          <label className="field">
            <span>Text</span>
            <input
              type="text"
              value={annotationText}
              onChange={(event) => setAnnotationText(event.target.value)}
              placeholder="Annotation text"
            />
          </label>

          <label className="upload upload-secondary">
            <span>Upload Stamp Image</span>
            <input
              type="file"
              accept="image/png,image/jpeg,image/webp"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) {
                  void uploadStampImage(file);
                  event.currentTarget.value = '';
                }
              }}
            />
          </label>

          <button type="button" onClick={() => void runAddAnnotation()} disabled={!activePage}>
            Add Annotation To Active Page
          </button>
          <button type="button" onClick={() => void runNudgeLastAnnotation()} disabled={!selectedAnnotation}>
            Move Selected Annotation
          </button>
          <button type="button" onClick={() => void runDeleteLastAnnotation()} disabled={!selectedAnnotation}>
            Delete Selected Annotation
          </button>
          <div className="annotation-list">
            {activePageAnnotations.map((item) => (
              <button
                key={item.id}
                type="button"
                className={selectedAnnotationId === item.id ? 'annotation-row active' : 'annotation-row'}
                onClick={() => setSelectedAnnotationId(item.id)}
              >
                {annotationLabel(item)}
              </button>
            ))}
            {activePageAnnotations.length === 0 && <p className="muted-small">No annotations on active page.</p>}
          </div>
          <p>{annotationStatus}</p>

          <h2>Export</h2>
          <label className="field">
            <span>Format</span>
            <select value={exportFormat} onChange={(event) => setExportFormat(event.target.value as ExportFormat)}>
              <option value="pdf">PDF</option>
              <option value="png">PNG</option>
              <option value="jpeg">JPEG</option>
            </select>
          </label>

          <label className="field">
            <span>Pages</span>
            <select value={exportTarget} onChange={(event) => setExportTarget(event.target.value as ExportTarget)}>
              <option value="all">All pages</option>
              <option value="active">Active page only</option>
            </select>
          </label>

          <label className="field">
            <span>DPI: {exportDpi}</span>
            <input
              type="range"
              min={72}
              max={300}
              step={1}
              value={exportDpi}
              onChange={(event) => setExportDpi(Number(event.target.value))}
            />
          </label>

          <label className="field checkbox-field">
            <input
              type="checkbox"
              checked={exportFlatten}
              onChange={(event) => setExportFlatten(event.target.checked)}
            />
            <span>Flatten before export</span>
          </label>

          <button type="button" onClick={() => void runExport()} disabled={!selectedDocumentId}>
            Export File
          </button>
          <p>{exportStatus}</p>

          <h2>Reduce File Size</h2>
          <p className="notice">Profiles mirror online tools: light, balanced, strong.</p>

          <label className="field">
            <span>Profile</span>
            <select
              value={compressProfile}
              onChange={(event) => setCompressProfile(event.target.value as CompressionProfile)}
            >
              <option value="light">Light (best quality)</option>
              <option value="balanced">Balanced (recommended)</option>
              <option value="strong">Strong (smallest size)</option>
            </select>
          </label>

          <label className="field">
            <span>Image DPI: {compressDpi}</span>
            <input
              type="range"
              min={72}
              max={300}
              step={1}
              value={compressDpi}
              onChange={(event) => setCompressDpi(Number(event.target.value))}
            />
          </label>

          <label className="field checkbox-field">
            <input
              type="checkbox"
              checked={compressStripMetadata}
              onChange={(event) => setCompressStripMetadata(event.target.checked)}
            />
            <span>Strip metadata</span>
          </label>

          <button type="button" onClick={() => void runCompress()} disabled={!selectedDocumentId}>
            Export Compressed PDF
          </button>

          <button type="button" onClick={() => void estimateCompress()} disabled={!selectedDocumentId}>
            Estimate Size Reduction
          </button>

          {compressEstimate && (
            <div className="estimate-box">
              <p>
                Estimated: {formatBytes(compressEstimate.sourceBytes)} to {formatBytes(compressEstimate.estimatedBytes)}
              </p>
              <p>Estimated reduction: {compressEstimate.estimatedReductionPercent.toFixed(1)}%</p>
              {compressEstimate.note && <p>{compressEstimate.note}</p>}
            </div>
          )}

          <p>{compressStatus}</p>
        </aside>
      </section>
    </main>
  );
}
