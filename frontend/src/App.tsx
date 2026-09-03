import { useMemo, useState } from 'react';

type DocumentItem = {
  id: string;
  title: string;
  original_name: string;
  page_count: number;
};

type DocumentsResponse = {
  items: DocumentItem[];
};

async function fetchDocuments(): Promise<DocumentItem[]> {
  const response = await fetch('/api/documents');
  if (!response.ok) {
    throw new Error('Failed to load documents');
  }
  const payload = (await response.json()) as DocumentsResponse;
  return payload.items;
}

export function App() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [items, setItems] = useState<DocumentItem[]>([]);

  const hasItems = useMemo(() => items.length > 0, [items]);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const next = await fetchDocuments();
      setItems(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="page">
      <header className="header">
        <h1>myOpenPDF</h1>
        <p>Milestone M0 starter: local library and API connectivity.</p>
      </header>

      <section className="panel">
        <button type="button" onClick={refresh} disabled={loading}>
          {loading ? 'Loading...' : 'Load Documents'}
        </button>

        {error && <p className="error">{error}</p>}

        {!loading && !hasItems && !error && <p>No documents yet. Upload through API to start.</p>}

        {hasItems && (
          <ul>
            {items.map((item) => (
              <li key={item.id}>
                <strong>{item.title}</strong> ({item.page_count} pages) - {item.original_name}
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
