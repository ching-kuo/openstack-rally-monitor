# Dashboard Theme Customization

The dashboard supports a narrow, upgrade-safe theme overlay. The shipped theme lives in `/dashboard/themes/default/`; operator overrides live in `/results/branding/`, exposed at runtime as `/dashboard/themes/custom`.

Each custom file is optional. Missing files fall back to the default theme. A malformed CSS declaration is ignored by the browser's CSS parser, so a partially broken `tokens.css` should not stop the dashboard from loading.

## Files You Can Override

Place any of these files in `/results/branding/`:

| File | Purpose |
| --- | --- |
| `tokens.css` | CSS custom properties from the public token contract below |
| `overrides.css` | Small extra stylesheet for visual polish |
| `logo.svg` | Header logo loaded into the 32x32 brand slot |
| `favicon.svg` | Browser tab icon |

Nothing else is part of the supported override surface.

## Mounting A Theme

Use one workflow, not both.

Populate the existing Docker volume:

```bash
docker cp ./my-theme/. rally-monitor:/results/branding/
docker restart rally-monitor
```

Or add a nested bind mount in `docker/docker-compose.yml`:

```yaml
services:
  rally-monitor:
    volumes:
      - rally-results:/results
      - ./my-theme:/results/branding:ro
```

Then rebuild or restart the container. The entrypoint recreates `/dashboard/themes/custom -> /results/branding` on each boot.

## Minimum Theme

`my-theme/tokens.css`:

```css
:root {
    --color-brand-primary: #ff00ff;
    --color-brand-secondary: #22d3ee;
    --gradient-brand: linear-gradient(135deg, #ff00ff, #22d3ee);
    --chart-series-1: #ff00ff;
}
```

## Public Token Contract

Custom themes may override these names. Maintainers should keep them stable across versions.

```css
:root {
    --color-surface-base: #0a0e1a;
    --color-surface-raised: #111827;
    --color-surface-card: rgba(17, 24, 39, 0.7);

    --color-text-primary: #f1f5f9;
    --color-text-secondary: #94a3b8;
    --color-text-muted: #64748b;

    --color-success: #10b981;
    --color-failure: #ef4444;
    --color-pending: #64748b;
    --color-warning: #f59e0b;

    --color-brand-primary: #6366f1;
    --color-brand-secondary: #06b6d4;

    --gradient-brand: linear-gradient(135deg, #6366f1, #06b6d4);
    --gradient-success: linear-gradient(135deg, #10b981, #059669);
    --gradient-failure: linear-gradient(135deg, #ef4444, #dc2626);

    --chart-series-1: #6366f1;
    --chart-series-2: #06b6d4;
    --chart-series-3: #10b981;
    --chart-series-4: #f59e0b;
    --chart-series-5: #ef4444;
    --chart-series-6: #8b5cf6;
    --chart-series-7: #ec4899;
    --chart-axis-label: #94a3b8;
    --chart-axis-tick: #64748b;
    --chart-grid: rgba(255,255,255,0.04);
}
```

## What Reskins

Token overrides reskin dashboard surfaces, text, status colors, brand gradients, timeline bars, service-card borders, cleanup counts, Chart.js datasets, chart labels, axis ticks, and chart grid lines.

The per-run **full report** (linked from each service card, served at `runs/<timestamp>/<service>.html`) loads the same token cascade, so the same overrides reskin its nav, tables, stat tiles, status pills, and the atomic-action chart for free.

Supplying `logo.svg` or `favicon.svg` replaces the default brand assets independently of token overrides.

> Chart series tokens (`--chart-series-1` … `--chart-series-7`) must be `#rrggbb` hex or `rgba(...)`. Other CSS color forms (`hsl(...)`, `oklch(...)`, named colors) render correctly for line strokes but lose the translucent fill under each duration line.

## What Does Not Reskin

Some decorative details are intentionally internal: status-chip alpha backgrounds such as `rgba(16, 185, 129, 0.12)`, glow shadows such as `rgba(99, 102, 241, 0.3)`, glass borders and scrollbar overlays, and the default SVG logo gradient. SVG files loaded through `<img>` do not inherit page CSS variables, so provide your own `logo.svg` to recolor the brand mark.

Out of scope for this version: a theme picker, multiple shipped presets, page title/header text overrides, environment banners, light-theme generation, and hot reload.
