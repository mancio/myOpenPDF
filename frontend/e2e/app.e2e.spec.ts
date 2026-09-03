import { expect, test } from '@playwright/test';

type RectTuple = [number, number, number, number];

function asRectTuple(value: unknown): RectTuple | null {
  if (!Array.isArray(value) || value.length !== 4) {
    return null;
  }
  const values = value.map((item) => Number(item));
  if (values.some((item) => Number.isNaN(item))) {
    return null;
  }
  return [values[0], values[1], values[2], values[3]];
}

function buildPdf(pageCount: number): Buffer {
  const pageObjects: string[] = [];
  const kids: string[] = [];

  for (let index = 0; index < pageCount; index += 1) {
    const pageObjectNumber = 4 + index * 2;
    const contentObjectNumber = pageObjectNumber + 1;
    kids.push(`${pageObjectNumber} 0 R`);

    const content = `BT\n/F1 12 Tf\n72 720 Td\n(E2E page ${index + 1}) Tj\nET\n`;
    pageObjects.push(
      `${pageObjectNumber} 0 obj\n` +
        `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents ${contentObjectNumber} 0 R >>\n` +
        `endobj\n`
    );
    pageObjects.push(
      `${contentObjectNumber} 0 obj\n` +
        `<< /Length ${content.length} >>\n` +
        `stream\n${content}endstream\n` +
        `endobj\n`
    );
  }

  const objects = [
    '1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n',
    `2 0 obj\n<< /Type /Pages /Kids [${kids.join(' ')}] /Count ${pageCount} >>\nendobj\n`,
    '3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n',
    ...pageObjects,
  ];

  const header = '%PDF-1.4\n';
  let body = '';
  const offsets: number[] = [0];

  for (const object of objects) {
    offsets.push(header.length + body.length);
    body += object;
  }

  const xrefStart = header.length + body.length;
  let xref = `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (let number = 1; number <= objects.length; number += 1) {
    xref += `${offsets[number].toString().padStart(10, '0')} 00000 n \n`;
  }

  const trailer =
    `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\n` +
    `startxref\n${xrefStart}\n%%EOF\n`;

  return Buffer.from(header + body + xref + trailer, 'ascii');
}

test('upload, annotate, export, and request cancel', async ({ page }) => {
  await page.route(/\/api\/documents\/[^/]+\/pages\/[^/]+\/thumb\?.*$/, async (route) => {
    await route.fulfill({ status: 204, body: '' });
  });

  await page.goto('/');

  const docName = `sample-e2e-${Date.now()}.pdf`;
  const docTitle = docName.replace(/\.pdf$/i, '');

  const uploadInput = page.locator('label.upload input[type="file"]').first();
  await uploadInput.setInputFiles({
    name: docName,
    mimeType: 'application/pdf',
    buffer: buildPdf(1),
  });

  const docButton = page.getByRole('button', { name: new RegExp(`${docTitle}.*1 pages`, 'i') }).first();
  await expect(docButton).toBeVisible();
  await docButton.click();
  await expect(docButton).toContainText('1 pages');

  const firstThumb = page.locator('.thumb').first();
  await expect(firstThumb).toBeVisible();
  await firstThumb.click();
  await expect(page.getByRole('button', { name: 'Add Annotation To Active Page' })).toBeEnabled();

  await page.getByLabel('Tool').selectOption('rect');
  await page.getByRole('button', { name: 'Add Annotation To Active Page' }).click();
  const annotationBox = page.locator('.annotation-box').first();
  await expect(annotationBox).toBeVisible();

  const initial = await annotationBox.boundingBox();
  if (!initial) {
    throw new Error('Annotation box is not measurable');
  }

  const dragPersisted = page.waitForRequest((request) => {
    if (request.method() !== 'POST' || !/\/api\/documents\/[^/]+\/ops$/.test(request.url())) {
      return false;
    }
    return request.postData()?.includes('"kind":"annot.update"') ?? false;
  });

  await page.mouse.move(initial.x + initial.width / 2, initial.y + initial.height / 2);
  await page.mouse.down();
  await page.mouse.move(initial.x + initial.width / 2 + 28, initial.y + initial.height / 2 + 18);
  await page.mouse.up();
  await dragPersisted;

  const brHandle = page.locator('.annotation-box-selected .annotation-handle.br').first();
  const handlePoint = await brHandle.boundingBox();
  if (!handlePoint) {
    throw new Error('Resize handle is not measurable');
  }

  await page.mouse.move(handlePoint.x + handlePoint.width / 2, handlePoint.y + handlePoint.height / 2);
  await page.mouse.down();
  await page.mouse.move(handlePoint.x + handlePoint.width / 2 + 30, handlePoint.y + handlePoint.height / 2 + 24);
  await page.mouse.up();

  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: 'Export File' }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/^export-.*\.pdf$/);

  const cancelResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' && /\/api\/jobs\/[^/]+\/cancel$/.test(response.url()),
    { timeout: 10_000 }
  );

  await page.getByRole('button', { name: 'Export Scanned PDF' }).click();
  await expect(page.getByRole('button', { name: 'Cancel Running Job' })).toBeEnabled();
  await page.getByRole('button', { name: 'Cancel Running Job' }).click();

  const cancelResponse = await cancelResponsePromise;
  expect(cancelResponse.status()).toBe(202);
});

test('persists corner resize with payload rect delta', async ({ page }) => {
  await page.route(/\/api\/documents\/[^/]+\/pages\/[^/]+\/thumb\?.*$/, async (route) => {
    await route.fulfill({ status: 204, body: '' });
  });

  let addedRect: RectTuple | null = null;
  const updatedRects: RectTuple[] = [];
  page.on('request', (request) => {
    if (request.method() !== 'POST' || !/\/api\/documents\/[^/]+\/ops$/.test(request.url())) {
      return;
    }

    let body: {
      kind?: string;
      payload?: { annot?: { rect?: unknown } };
    };
    try {
      body = request.postDataJSON() as {
        kind?: string;
        payload?: { annot?: { rect?: unknown } };
      };
    } catch {
      return;
    }

    if (body.kind === 'annot.add') {
      const rect = asRectTuple(body.payload?.annot?.rect);
      if (rect) {
        addedRect = rect;
      }
      return;
    }

    if (body.kind === 'annot.update') {
      const rect = asRectTuple(body.payload?.annot?.rect);
      if (rect) {
        updatedRects.push(rect);
      }
    }
  });

  await page.goto('/');

  const docName = `sample-e2e-resize-${Date.now()}.pdf`;
  const docTitle = docName.replace(/\.pdf$/i, '');

  const uploadInput = page.locator('label.upload input[type="file"]').first();
  await uploadInput.setInputFiles({
    name: docName,
    mimeType: 'application/pdf',
    buffer: buildPdf(1),
  });

  const docButton = page.getByRole('button', { name: new RegExp(`${docTitle}.*1 pages`, 'i') }).first();
  await expect(docButton).toBeVisible();
  await docButton.click();

  const firstThumb = page.locator('.thumb').first();
  await expect(firstThumb).toBeVisible();
  await firstThumb.click();
  await expect(page.getByRole('button', { name: 'Add Annotation To Active Page' })).toBeEnabled();

  await page.getByLabel('Tool').selectOption('rect');
  await page.getByRole('button', { name: 'Add Annotation To Active Page' }).click();

  const annotationBox = page.locator('.annotation-box-selected').first();
  await expect(annotationBox).toBeVisible();

  const brHandle = annotationBox.locator('.annotation-handle.br').first();
  const handlePoint = await brHandle.boundingBox();
  if (!handlePoint) {
    throw new Error('Resize handle is not measurable');
  }

  const resizePersisted = page.waitForRequest((request) => {
    if (request.method() !== 'POST' || !/\/api\/documents\/[^/]+\/ops$/.test(request.url())) {
      return false;
    }
    return request.postData()?.includes('"kind":"annot.update"') ?? false;
  });

  await page.mouse.move(handlePoint.x + handlePoint.width / 2, handlePoint.y + handlePoint.height / 2);
  await page.mouse.down();
  await page.mouse.move(handlePoint.x + handlePoint.width / 2 + 34, handlePoint.y + handlePoint.height / 2 + 26);
  await page.mouse.up();
  await resizePersisted;

  await expect.poll(() => updatedRects.length).toBeGreaterThan(0);
  expect(addedRect).not.toBeNull();
  const base = addedRect as RectTuple;
  const resized = updatedRects[updatedRects.length - 1];

  const baseWidth = base[2] - base[0];
  const baseHeight = base[3] - base[1];
  const resizedWidth = resized[2] - resized[0];
  const resizedHeight = resized[3] - resized[1];

  expect(resizedWidth).toBeGreaterThan(baseWidth + 1);
  expect(resizedHeight).toBeGreaterThan(baseHeight + 1);
});
