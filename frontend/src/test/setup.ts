import '@testing-library/jest-dom/vitest';

Object.defineProperty(globalThis.HTMLCanvasElement.prototype, 'getContext', {
  value: () => ({
    setTransform: () => undefined,
  }),
});

Object.defineProperty(globalThis.HTMLAnchorElement.prototype, 'click', {
  value: () => undefined,
});
