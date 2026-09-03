import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { vi } from 'vitest';

import { App } from './App';

vi.mock('pdfjs-dist', () => {
  const page = {
    getViewport: () => ({ width: 595, height: 842 }),
    render: () => ({ promise: Promise.resolve(), cancel: () => undefined }),
  };

  return {
    GlobalWorkerOptions: { workerSrc: '' },
    getDocument: () => ({
      promise: Promise.resolve({
        getPage: async () => page,
        destroy: async () => undefined,
      }),
      destroy: () => undefined,
    }),
  };
});

type MockState = {
  version: number;
  pageCount: number;
  annotations: Array<{
    id: string;
    page: string;
    kind: string;
    rect: [number, number, number, number];
    text: string | null;
    asset_id: string | null;
  }>;
  jobPolls: number;
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function createFetchMock(state: MockState) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const rawUrl = typeof input === 'string' ? input : input.toString();
    const url = new URL(rawUrl, 'http://127.0.0.1:5173');
    const method = init?.method ?? 'GET';

    if (method === 'GET' && url.pathname === '/api/documents') {
      return jsonResponse({
        items: [
          {
            id: 'doc-1',
            title: 'Sample',
            original_name: 'sample.pdf',
            page_count: state.pageCount,
            version: state.version,
          },
        ],
      });
    }

    if (method === 'GET' && url.pathname === '/api/documents/doc-1') {
      return jsonResponse({
        id: 'doc-1',
        title: 'Sample',
        original_name: 'sample.pdf',
        page_count: state.pageCount,
        version: state.version,
      });
    }

    if (method === 'GET' && url.pathname === '/api/documents/doc-1/pages') {
      return jsonResponse(
        Array.from({ length: state.pageCount }, (_, index) => ({
          uuid: `page-${index + 1}`,
          index,
          width: 595,
          height: 842,
          rotation: 0,
          has_text: true,
          label: `${index + 1}`,
        }))
      );
    }

    if (method === 'GET' && url.pathname === '/api/documents/doc-1/forms') {
      return jsonResponse([]);
    }

    if (method === 'GET' && url.pathname === '/api/documents/doc-1/annotations') {
      return jsonResponse(state.annotations);
    }

    if (method === 'POST' && url.pathname === '/api/documents/doc-1/ops') {
      const payload = JSON.parse(String(init?.body ?? '{}')) as {
        kind: string;
        payload: Record<string, unknown>;
      };

      if (payload.kind === 'annot.add') {
        const annot = payload.payload.annot as {
          id: string;
          page: string;
          kind: string;
          rect: [number, number, number, number];
          text?: string;
          asset_id?: string;
        };
        state.annotations = [
          {
            id: annot.id,
            page: annot.page,
            kind: annot.kind,
            rect: annot.rect,
            text: annot.text ?? null,
            asset_id: annot.asset_id ?? null,
          },
        ];
      }

      if (payload.kind === 'annot.update') {
        const annot = payload.payload.annot as {
          id: string;
          rect: [number, number, number, number];
          text?: string;
        };
        state.annotations = state.annotations.map((item) =>
          item.id === annot.id ? { ...item, rect: annot.rect, text: annot.text ?? item.text } : item
        );
      }

      if (payload.kind === 'annot.delete') {
        const annotId = payload.payload.id as string;
        state.annotations = state.annotations.filter((item) => item.id !== annotId);
      }

      state.version += 1;
      return jsonResponse({ version: state.version, cursor: state.version });
    }

    if (method === 'POST' && url.pathname === '/api/documents/doc-1/export') {
      state.jobPolls = 0;
      return jsonResponse({ id: 'job-1', status: 'queued', progress: 0, message: null });
    }

    if (method === 'POST' && url.pathname === '/api/documents/doc-1/undo') {
      state.version = Math.max(0, state.version - 1);
      return jsonResponse({ version: state.version, cursor: state.version });
    }

    if (method === 'POST' && url.pathname === '/api/documents/doc-1/redo') {
      state.version += 1;
      return jsonResponse({ version: state.version, cursor: state.version });
    }

    if (method === 'GET' && url.pathname === '/api/jobs/job-1') {
      state.jobPolls += 1;
      if (state.jobPolls < 2) {
        return jsonResponse({ id: 'job-1', status: 'running', progress: 0.4, message: 'rendering' });
      }
      return jsonResponse({ id: 'job-1', status: 'done', progress: 1, message: 'Export completed.' });
    }

    if (method === 'POST' && url.pathname === '/api/jobs/job-1/cancel') {
      return jsonResponse({ accepted: true }, 202);
    }

    return new Response('not found', { status: 404 });
  });
}

describe('App flows', () => {
  test('loads persisted annotations and edits by selection', async () => {
    const state: MockState = { version: 0, pageCount: 1, annotations: [], jobPolls: 0 };
    globalThis.fetch = createFetchMock(state);

    render(<App />);

    await screen.findByText('Sample');
    const addButton = await screen.findByRole('button', {
      name: 'Add Annotation To Active Page',
    });
    fireEvent.click(addButton);

    await waitFor(() => {
      expect(screen.getByText('highlight')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Move Selected Annotation' }));
    await waitFor(() => {
      expect(screen.getByText('Moved selected annotation')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Delete Selected Annotation' }));
    await waitFor(() => {
      expect(screen.getByText('No annotations on active page.')).toBeInTheDocument();
    });
  });

  test('completes async export by polling job progress', async () => {
    const state: MockState = { version: 0, pageCount: 1, annotations: [], jobPolls: 0 };
    globalThis.fetch = createFetchMock(state);

    render(<App />);
    await screen.findByText('Sample');

    fireEvent.click(screen.getByRole('button', { name: 'Export File' }));

    await waitFor(() => {
      expect(screen.getByText('Export completed.')).toBeInTheDocument();
    });
  });

  test('shows chunked thumbnail loading hint for large documents', async () => {
    const state: MockState = { version: 0, pageCount: 120, annotations: [], jobPolls: 0 };
    globalThis.fetch = createFetchMock(state);

    render(<App />);
    await screen.findByText('Sample');

    await waitFor(() => {
      expect(screen.getByText('Loaded 60 of 120 thumbnails. Scroll to load more.')).toBeInTheDocument();
    });
  });

  test('supports dragging annotation geometry directly in viewer', async () => {
    const state: MockState = { version: 0, pageCount: 1, annotations: [], jobPolls: 0 };
    const fetchMock = createFetchMock(state);
    globalThis.fetch = fetchMock;

    const { container } = render(<App />);
    await screen.findByText('Sample');

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Add Annotation To Active Page' })).toBeEnabled();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Add Annotation To Active Page' }));

    await waitFor(() => {
      expect(container.querySelector('.annotation-box')).not.toBeNull();
    });
    const box = container.querySelector('.annotation-box') as HTMLElement;

    fireEvent.pointerDown(box, { pointerId: 1, clientX: 120, clientY: 120 });
    fireEvent.pointerMove(window, { pointerId: 1, clientX: 152, clientY: 142 });
    fireEvent.pointerUp(window, { pointerId: 1, clientX: 152, clientY: 142 });

    await waitFor(() => {
      const didPersistUpdate = fetchMock.mock.calls.some(([, init]) => {
        if (!init || init.method !== 'POST') {
          return false;
        }
        return typeof init.body === 'string' && init.body.includes('"kind":"annot.update"');
      });
      expect(didPersistUpdate).toBe(true);
    });
  });

  test('supports search focus and undo/redo keyboard shortcuts', async () => {
    const state: MockState = { version: 0, pageCount: 1, annotations: [], jobPolls: 0 };
    const fetchMock = createFetchMock(state);
    globalThis.fetch = fetchMock;

    render(<App />);
    await screen.findByText('Sample');

    const searchInput = screen.getByLabelText('Search text') as HTMLInputElement;
    expect(document.activeElement).not.toBe(searchInput);

    fireEvent.keyDown(document, { key: 'f', ctrlKey: true });
    await waitFor(() => {
      expect(document.activeElement).toBe(searchInput);
    });

    fireEvent.keyDown(document, { key: 'z', ctrlKey: true });
    fireEvent.keyDown(document, { key: 'z', ctrlKey: true, shiftKey: true });

    await waitFor(() => {
      const calledUndo = fetchMock.mock.calls.some(([rawUrl, init]) => {
        if (!init || init.method !== 'POST') {
          return false;
        }
        const url = new URL(typeof rawUrl === 'string' ? rawUrl : rawUrl.toString(), 'http://127.0.0.1:5173');
        return url.pathname === '/api/documents/doc-1/undo';
      });
      const calledRedo = fetchMock.mock.calls.some(([rawUrl, init]) => {
        if (!init || init.method !== 'POST') {
          return false;
        }
        const url = new URL(typeof rawUrl === 'string' ? rawUrl : rawUrl.toString(), 'http://127.0.0.1:5173');
        return url.pathname === '/api/documents/doc-1/redo';
      });
      expect(calledUndo).toBe(true);
      expect(calledRedo).toBe(true);
    });
  });
});
