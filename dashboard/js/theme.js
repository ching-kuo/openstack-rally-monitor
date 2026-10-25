/**
 * Custom theme assets: swap in themes/custom/ logo and favicon when present,
 * and drop the optional custom stylesheet links that failed to load.
 */

// Whether a same-origin asset exists (a non-2xx or a network error is "no").
async function exists(url) {
  try {
    return (await fetch(url, { cache: "no-store" })).ok;
  } catch {
    return false;
  }
}

export async function resolveThemeAssets() {
  const assets = [
    {
      id: "brandLogo",
      attr: "src",
      customPath: "themes/custom/logo.svg",
      defaultPath: "themes/default/logo.svg",
    },
    {
      id: "favicon",
      attr: "href",
      customPath: "themes/custom/favicon.svg",
      defaultPath: "themes/default/favicon.svg",
    },
  ];

  await Promise.all(
    assets.map(async ({ id, attr, customPath, defaultPath }) => {
      const element = document.getElementById(id);
      if (!element) return;
      element.setAttribute(attr, (await exists(customPath)) ? customPath : defaultPath);
    }),
  );
}

// Remove the optional custom-theme <link> tags whose files are absent.
// Replaces the former inline onerror="this.remove()" handlers, which had to
// go so the CSP could drop 'unsafe-inline' from script-src. A dangling link
// is already inert (the 404 is text/plain and nosniff blocks it as CSS), so
// this is cleanup, not correctness. A link without a `sheet` failed, but only
// once loading has finished: its error event may fire before the modules
// run, so the check waits for the window load event, which follows every
// stylesheet's load or error (same sheet test as report.js). No re-download,
// and a transient error during this page load is the browser's own verdict.
export function pruneFailedThemeLinks() {
  const prune = () => {
    for (const link of document.querySelectorAll("link.custom-theme-css")) {
      if (link.sheet === null) link.remove();
    }
  };
  if (document.readyState === "complete") prune();
  else window.addEventListener("load", prune, { once: true });
}
