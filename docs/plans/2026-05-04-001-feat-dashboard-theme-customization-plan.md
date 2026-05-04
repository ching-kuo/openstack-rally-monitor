---
title: "feat: Dashboard theme customization (default theme + custom bind-mount slot)"
type: feat
status: active
date: 2026-05-04
origin: docs/brainstorms/2026-05-04-dashboard-theme-customization-requirements.md
---

# Dashboard Theme Customization

## Summary

Implement the Horizon-style portable theme contract on the static dashboard: extract the current look into `dashboard/themes/default/` (tokens, logo, favicon), introduce a `dashboard/themes/custom/` slot symlinked into the existing `/results/` volume for optional operator overrides, refactor `:root` to a small set of semantic token names, and rewire `dashboard/app.js` so Chart.js datasets and chrome read those tokens at render time so a custom theme reskins charts coherently.

---

## Problem Frame

The dashboard currently hardcodes its visual identity in three places that have drifted into separate spheres: `:root` in `dashboard/style.css` for HTML/CSS, an inline SVG with a hardcoded gradient in `dashboard/index.html` for the logo, and per-call hex literals in `dashboard/app.js` for Chart.js datasets and axis chrome. There is no documented, upgrade-safe override path. Operators familiar with OpenStack Horizon expect a `themes/<name>/` directory contract to exist and reach for it first. (See origin: `docs/brainstorms/2026-05-04-dashboard-theme-customization-requirements.md`.)

---

## Requirements

- R1. Ship a `dashboard/themes/default/` directory that holds the current look — tokens, optional style overrides, logo, favicon. CSS custom properties only, no SCSS, no build step.
- R2. A `dashboard/themes/custom/` slot exists as the operator-supplied override target. Empty in the shipped image; activates by file presence when an operator bind-mounts content.
- R3. Custom theme is partial overlay — files the custom theme does not provide fall through to the default transparently.
- R4. Override surface is exactly: token definitions, an optional extra stylesheet, the logo, the favicon. Nothing else.
- R5. The maintainer-owned token contract is documented and stable; the public contract is intentionally narrow.
- R6. Activation is auto-detect (file presence in `custom/`), not env-var-gated.
- R7. The bind-mount target lives under the existing rally-results Docker volume; the symlink is established at container startup by `scripts/entrypoint.sh`.
- R8. Broken or partially populated custom theme degrades gracefully — missing files fall through; a malformed token file does not break the dashboard load.
- R9. `:root` in `dashboard/style.css` carries semantic token names (success, failure, surface, text, chart series, …). Existing values stay byte-equivalent.
- R10. `dashboard/app.js` reads chart dataset and chart-chrome colors from semantic tokens at render time via `getComputedStyle` — no hardcoded hex in JS chart code. (Decorative alpha-tint literals in `dashboard/style.css` stay internal and intentionally do not reskin under a custom theme; documented in CUSTOMIZING.md.)
- R11. Chart instances pick up new colors on next refresh after a token change (no mid-render swap requirement).
- R12. `dashboard/serve.py`'s allowlist is extended to permit serving files under the `themes/` subtree. Path-traversal rejection and explicit-allowlist posture stay intact.
- R13. Correct MIME types and no inline `<style>` injection in `dashboard/index.html`.
- R14. `README.md`, `CLAUDE.md`, and `docs/CUSTOMIZING.md` document the override contract, bind-mount path, fallback semantics, what reskins vs what does not, and the minimum viable custom theme.

**Origin actors:** A1 (Operator deploying the container), A2 (Operator viewing the dashboard), A3 (Maintainer of this repo).
**Origin acceptance examples:** AE1 (R6, R7), AE2 (R3, R6), AE3 (R10, R11), AE4 (R8), AE5 (R12).

---

## Scope Boundaries

- Multi-preset theme library (`high-contrast`, `openstack-native`, `prod-red`, `staging-amber`).
- In-dashboard theme picker UI.
- Per-user theme persistence via `localStorage`.
- Env-var theme registry (`DASHBOARD_AVAILABLE_THEMES` / `DEFAULT_THEME` / `SELECTABLE_THEMES`).
- Customizing page title, header copy, service list, links, banners, or non-visual text.
- Environment-disambiguation banner.
- Single-accent-env-var palette derivation (HSL math, contrast guard, auto-favicon generation).
- Web App Manifest / PWA support and `.webmanifest` serving.
- Hot reload of theme files without container restart.
- Light theme generation, automatic dark/light pairs, `prefers-color-scheme`.
- SCSS or any build step.
- WCAG contrast validation at startup, fail-closed on broken custom themes.
- Tokenization of decorative alpha tints and shadow rgba() literals in `dashboard/style.css` — these stay internal; only the public contract reskins.

---

## Context & Research

### Relevant Code and Patterns

- `dashboard/style.css` lines 24-65 — current `:root` palette to refactor with semantic names; values stay byte-equivalent.
- `dashboard/style.css` lines 89, 173, 178, 191, 197, 300, 305, 311, 314, 422, 470, 471, 728, 729, 815, 826, 853, 858, 962, 967 — decorative `rgba(...)` literals for status tints, glows, grids, and shadows. These stay as-is per R10 narrowing — they are not part of the public theme contract.
- `dashboard/index.html` lines 21-32 — inline SVG logo with hardcoded `#6366f1`/`#06b6d4` linear gradient; replace with `<img>` referencing a static SVG file in the active theme. Note: `<img>`-loaded SVG does not inherit page CSS variables (acknowledged limitation; documented in CUSTOMIZING.md).
- `dashboard/app.js` lines 462-470 and 541-549 — hardcoded chart color arrays; lines 499, 508, 515-519, 589, 599 — hardcoded chart axis/legend chrome colors. `SERVICE_ICONS` (line 14) are emoji strings, not colorized — out of scope.
- `dashboard/serve.py` lines 16-23 — `ALLOWED_STATIC` and `ALLOWED_JSON_SYMLINKS` allowlists; lines 77-83 — containment check via `relative_to`. Path-traversal already blocked at lines 57-59. New branch for `themes/` prefix needed.
- `dashboard/serve.py` lines 31-35 — CSP. `style-src 'self' 'unsafe-inline'` and `img-src 'self' data:` already permit theme assets without CSP changes.
- `dashboard/serve.py` lines 49-72 — `do_GET` only; no `do_HEAD`. Theme asset probes in U6 use GET with `cache: 'no-store'`, not HEAD, to match what the server actually answers.
- `scripts/entrypoint.sh` line 7 — `set -euo pipefail`. Failures abort startup. Theme symlink setup follows that posture (no special "warn and continue" handling).
- `scripts/entrypoint.sh` lines 145-148 — existing pattern for `ln -sf "${RESULTS_DIR}/X" /dashboard/X`. Use `ln -sfn` for the directory case.
- `scripts/entrypoint.sh` lines 49-50 — `mkdir -p "${RESULTS_DIR}"` is an existing first-boot safety; extend to create `${RESULTS_DIR}/branding`.
- `dashboard/test_serve.py` — pytest test patterns with tmpdir fixtures, ephemeral HTTP server, and `monkeypatch` of `SERVE_ROOT`/`RESULTS_ROOT`. New theme tests follow the same shape.
- `docker/Dockerfile` line 38 — `COPY dashboard/ /dashboard/`. The new `themes/default/` subtree ships via the same COPY; no Dockerfile change needed.
- `docker/docker-compose.yml` line 18 — existing `rally-results:/results` volume mount. Custom-theme operators populate `${rally-results}/branding/` or layer a nested bind mount; both documented in CUSTOMIZING.md.

### Institutional Learnings

- No `docs/solutions/` entries exist yet (confirmed during ideation grounding).

### External References

- OpenStack Horizon themes contract — `docs.openstack.org/horizon/latest/configuration/themes.html`. We mirror the directory shape and drop the SCSS compilation, Django templates, and `COMPRESS_OFFLINE` machinery.
- W3C CSS custom properties — `:root` cascade with later-declared values overriding earlier ones is the partial-overlay primitive.
- Chart.js color resolution — colors are read from dataset config at construction time; replacing the chart instance via `.destroy()` + `new Chart(...)` (already done in `renderCharts`/`renderHealthChart`) is sufficient for token-change pickup.

---

## Key Technical Decisions

- **Layered token cascade via two `<link>` tags, default-first then optional custom.** Default theme's `tokens.css` is render-blocking and unconditional; custom theme's `tokens.css` is render-blocking but its absence is harmless (browser logs a 404, no visual error). CSS variable cascade handles the override automatically — no JS, no FOUC. Satisfies R3 (partial overlay) and R8 (graceful degradation).
- **Logo/favicon resolution via JS GET probe at boot.** A small `resolveThemeAssets()` function fires before first chart render, issues `fetch(customPath, { cache: 'no-store' })` for `themes/custom/logo.svg` and `themes/custom/favicon.svg`, and points the `<img>` and `<link rel="icon">` at custom or default. GET is used (not HEAD) because `dashboard/serve.py` only implements `do_GET`. Probe payload is small (logo SVG, kilobytes) and runs once at boot.
- **Narrow public token contract; decorative alpha stays internal.** The public surface is grouped into families (surfaces, text, status, brand, gradients, chart series, chart chrome) — see U1 for the exact list. Decorative alpha-tint rgba()s scattered across `dashboard/style.css` stay as literals in `themes/default/tokens.css` and are not part of the public contract. A custom theme overriding `--color-success` will reskin status chips that read `var(--color-success)` but will not shift the `rgba(16, 185, 129, 0.08)` background tint behind those chips. This is the explicit price of "not fully customizable" and is documented in CUSTOMIZING.md under "What reskins vs what does not." Alternative considered: `color-mix(in srgb, ...)` derived alpha tokens — rejected as over-investment for a 10-operator internal tool.
- **`serve.py` allowlist extension is prefix + extension whitelist + tight containment.** A new branch accepts paths under `themes/` whose suffix (checked via `PurePosixPath(rel_str).suffix`, on the requested path, not the symlink target) is in `{".css", ".svg", ".png", ".ico"}` and whose resolved `target` is under `SERVE_ROOT/themes/` (default theme files) or under `${RESULTS_ROOT}/branding/` (custom theme via symlink — strictly tighter than the existing JSON branch's whole-`/results/` permit, by design). Path-traversal rejection on `..` parts and the explicit-set posture remain. `.webmanifest` is excluded — PWA support is out of scope.
- **Bind-mount path is `${RESULTS_DIR}/branding/`, symlinked to `dashboard/themes/custom`.** Reuses the existing `/results/` Docker volume — operators are already mounting it (`rally-results:/results`), so customization adds no new top-level volume. Two operator workflows are documented: (a) populate `${rally-results}/branding/` directly via `docker cp` or in-container shell; (b) add a nested `-v ./my-theme:/results/branding:ro` to the compose file. Both work; CUSTOMIZING.md shows both.
- **Token rename keeps current values byte-equivalent in U1, then rule bodies migrate in U3.** The semantic tokens land alongside the current ones in U1; current rule bodies still reference the old names. U3 migrates rule bodies and drops legacy names. Guarantees AE1 (default theme byte-equivalent to today) at the U1 boundary; visual regression risk is concentrated in U3 where it can be smoke-tested in isolation.
- **Chart token reads happen at render time, not at module load.** `renderCharts` / `renderHealthChart` already destroy and reconstruct charts on every refresh. Reading `getComputedStyle(...).getPropertyValue('--chart-series-1')` inside those functions makes R11 work mechanically without any token-change observer.
- **Logo container has explicit dimensions and clipping.** `.logo` and `#brandLogo` get `width: 32px; height: 32px; object-fit: contain; overflow: hidden` so a custom logo with unusual aspect ratio cannot break header layout. Added in U3 alongside the inline-SVG → `<img>` swap.
- **`set -euo pipefail` posture preserved.** Theme symlink failures abort startup, matching the rest of `entrypoint.sh`. No special "warn and continue" handling; that contradicts the existing posture and would obscure misconfigurations.

---

## Open Questions

### Resolved During Planning

- **Activation contract** — auto-detect by `themes/custom/tokens.css` presence (not env-var-gated). Resolved per origin's R6.
- **Bind-mount path** — `${RESULTS_DIR}/branding/`. Resolved here per the existing `/results/` symlink pattern (R7).
- **Token contract scope** — narrow public surface; decorative alpha stays internal. Resolved here.
- **Allowlist extension shape** — prefix + extension whitelist + containment narrowed to `${RESULTS_ROOT}/branding/`. Resolved here.
- **Asset probe HTTP method** — GET with `cache: 'no-store'`, not HEAD. Server only implements `do_GET`. Resolved here.
- **Chart token-pickup mechanism** — automatic via existing `destroy()` + `new Chart(...)` cycle. Resolved here.
- **Logo container sizing** — explicit `width: 32px; height: 32px; object-fit: contain` in U3. Resolved here.

### Deferred to Implementation

- Whether to ship a `themes/custom.example/` starter directory in-repo. Default plan position: do not — keep `custom/` strictly empty in the image. Document the contract in `docs/CUSTOMIZING.md` instead. Implementer may add an example if it materially helps users.

---

## Output Structure

```
dashboard/
  index.html              # modified
  style.css               # modified (rule bodies migrated to semantic tokens)
  app.js                  # modified (token reads + theme-asset resolver)
  serve.py                # modified (themes/ allowlist branch)
  test_serve.py           # modified (themes/ allowlist tests)
  themes/                 # NEW
    default/              # NEW (ships in image)
      tokens.css          # NEW (extracted from style.css :root)
      logo.svg            # NEW (extracted from index.html inline SVG)
      favicon.svg         # NEW (derived from logo)
    custom/               # NEW — symlink target at runtime, never shipped with content

scripts/
  entrypoint.sh           # modified (themes/custom symlink + branding dir)

docs/
  CUSTOMIZING.md          # NEW (operator-facing contract doc)
README.md                 # modified
CLAUDE.md                 # modified
```

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

**Token cascade (CSS load order in `dashboard/index.html`):**

```
<link rel="stylesheet" href="themes/default/tokens.css?v=...">  (always)
<link rel="stylesheet" href="style.css?v=...">                  (structural rules)
<link rel="stylesheet" href="themes/custom/tokens.css?v=..."    (optional)
      onerror="this.remove()">
<link rel="stylesheet" href="themes/custom/overrides.css?v=..." (optional)
      onerror="this.remove()">
```

**Asset resolution (in `dashboard/app.js`, runs before first chart render):**

```
on DOMContentLoaded:
  for each asset in [logo.svg, favicon.svg]:
    try: response = await fetch('themes/custom/<asset>', { cache: 'no-store' })
         if response.ok: set element src/href to themes/custom/<asset>
         else:           set element src/href to themes/default/<asset>
    catch: set element src/href to themes/default/<asset>
```

**Chart token resolution pattern (replaces hardcoded hex arrays in `renderCharts` / `renderHealthChart`):**

```
function readToken(name) {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
}

const seriesColors = [
  readToken('--chart-series-1'),
  readToken('--chart-series-2'),
  ...
];
const axisColor  = readToken('--chart-axis-tick');
const labelColor = readToken('--chart-axis-label');
const gridColor  = readToken('--chart-grid');
```

**Symlink topology at runtime (established by `scripts/entrypoint.sh`):**

```
/dashboard/themes/default/          (from image, read-only at runtime)
/dashboard/themes/custom -> /results/branding/   (symlink, recreated each boot)
/results/branding/                  (operator bind-mount target; may be empty)
```

---

## Implementation Units

> **Execution order:** U1 → U2 → U4 → U3 → U5 → U6 → U7. U-IDs are stable; the numeric order is preserved from initial drafting and does not match execution order. U4 lands before U3 because `dashboard/serve.py` must accept `themes/` paths before `dashboard/index.html` references them.

### U1. Add semantic token layer to `:root` in `dashboard/style.css`

**Goal:** Introduce a stable, documented set of semantic token names alongside the current `:root` values without changing any rendered pixel.

**Requirements:** R5, R9.

**Dependencies:** None.

**Files:**
- Modify: `dashboard/style.css`

**Approach:**
- Inside `:root`, add the public semantic-token block. Keep the existing tokens (`--bg-primary`, `--accent-indigo`, etc.) so current rule bodies continue working — the migration to semantic names happens in U3.
- Document the contract in a comment block at the top of `:root`: which names are public and stable, which are internal.

**Public token contract (the narrow surface custom themes may override; grouped by family):**
- Surfaces: `--color-surface-base`, `--color-surface-raised`, `--color-surface-card`
- Text: `--color-text-primary`, `--color-text-secondary`, `--color-text-muted`
- Status: `--color-success`, `--color-failure`, `--color-pending`, `--color-warning`
- Brand: `--color-brand-primary`, `--color-brand-secondary`
- Gradients: `--gradient-brand`, `--gradient-success`, `--gradient-failure`
- Chart series (7 slots, used round-robin): `--chart-series-1` … `--chart-series-7`
- Chart chrome: `--chart-axis-label`, `--chart-axis-tick`, `--chart-grid`

**Internal tokens (not part of public contract; live in default tokens.css; do not reskin under a custom theme):**
- Decorative alpha tints (status chip backgrounds, glow shadows, glass borders, scrollbar rgba)
- Radius / shadow / transition / font tokens
- Logo halo gradient stops (used by the `pulse-glow` keyframe)

**Patterns to follow:** existing `:root` block in `dashboard/style.css`.

**Test scenarios:**
- Test expectation: none — pure additive token declaration, no rule-body changes. Visual regression caught in U3.

**Verification:**
- The dashboard renders byte-equivalent to `main` after this unit lands.
- Every documented public-contract token is defined in `:root` with a value resolvable by `getComputedStyle`.

---

### U2. Refactor `dashboard/app.js` charts to read tokens at render time

**Goal:** Eliminate hardcoded chart and chrome hex literals; read everything from semantic tokens.

**Requirements:** R10, R11.

**Dependencies:** U1.

**Files:**
- Modify: `dashboard/app.js`

**Approach:**
- Add a `readToken(name)` helper that wraps `getComputedStyle(document.documentElement).getPropertyValue(name).trim()`.
- Replace the two `colors` arrays at lines 462-470 and 541-549 with calls that build the array from `--chart-series-1` … `--chart-series-7` via `readToken`.
- Replace hardcoded axis/legend/grid colors (`#94a3b8`, `#64748b`, `rgba(255,255,255,0.04)`) with `readToken('--chart-axis-label')`, `readToken('--chart-axis-tick')`, `readToken('--chart-grid')`.
- Read tokens inside the chart-render functions, not at module top level — guarantees R11 picks up custom-theme overrides on the next render cycle without observer plumbing.
- `SERVICE_ICONS` (line 14) is emoji and unchanged — out of scope.

**Patterns to follow:** existing `renderCharts(history)` and `renderHealthChart(healthHistory)` structure; both already destroy and reconstruct on each refresh.

**Test scenarios:**
- Happy path: charts render correctly under default tokens after the refactor; visual smoke test in browser.
- Integration: a custom theme overriding `--chart-series-1` on `:root` produces charts whose first dataset uses the overridden color on the next refresh cycle. Verified by manual container test (see Verification). **Covers AE3.**

**Verification:**
- Run the dashboard with the default theme; confirm charts look identical to `main`.
- Mount a custom theme containing `tokens.css` with `:root { --chart-series-1: #ff00ff; }` and confirm the first chart series renders magenta after the next 5-minute refresh (or manual reload).

---

### U4. Extend `dashboard/serve.py` allowlist to permit `themes/` subtree

**Goal:** Add a third allowlist branch for theme assets without weakening path-traversal or containment.

**Requirements:** R12, R13.

**Dependencies:** None. Lands before U3 so `dashboard/index.html` can reference `themes/default/*` without 403s.

**Files:**
- Modify: `dashboard/serve.py`
- Modify: `dashboard/test_serve.py`

**Approach:**
- Introduce constants: `THEME_PREFIX = "themes/"`, `ALLOWED_THEME_SUFFIXES = {".css", ".svg", ".png", ".ico"}` (no `.webmanifest` — PWA out of scope), and a `BRANDING_ROOT = (RESULTS_ROOT / "branding").resolve()` path.
- Add a third branch in `do_GET`: when `rel_str.startswith(THEME_PREFIX)` AND `PurePosixPath(rel_str).suffix in ALLOWED_THEME_SUFFIXES` AND the resolved `target` satisfies (`target.is_relative_to(SERVE_ROOT / "themes")` OR `target.is_relative_to(BRANDING_ROOT)`), serve it. Otherwise 403.
- Suffix is checked on the requested path (`rel_str`), not on the symlink target — symlinked theme files cannot mask their type.
- Containment for custom-theme paths is tightened to `${RESULTS_ROOT}/branding/`, strictly narrower than the existing JSON branch's `RESULTS_ROOT` permit. Symlinked theme files cannot expose arbitrary files elsewhere under `/results/`.
- The `..`-rejection at lines 57-59 already covers traversal attempts.
- Set `Cache-Control: public, max-age=300` on theme files (same as other static assets).

**Patterns to follow:** existing two-branch logic at lines 74-83 of `dashboard/serve.py`.

**Execution note:** Add tests for the new allowlist branch alongside the implementation — security-touching code benefits from coverage landing in the same commit. Test fixtures must `monkeypatch.setattr(serve, "BRANDING_ROOT", ...)` alongside the existing `SERVE_ROOT` and `RESULTS_ROOT` patches; otherwise tmp-dir tests will accidentally validate against the real `/results/branding/`.

**Test scenarios:**
- Happy path: `GET /themes/default/tokens.css` returns 200 with `text/css` MIME type when the file exists under `SERVE_ROOT/themes/default/`.
- Happy path: `GET /themes/custom/tokens.css` returns 200 when `themes/custom` is a symlink into `${RESULTS_ROOT}/branding/` and the file exists there.
- Happy path: `GET /themes/default/logo.svg` returns 200 with `image/svg+xml`.
- Edge case: `GET /themes/custom/tokens.css` returns 404 when the file is absent (no symlink target). Browser logs a benign 404; dashboard still loads.
- Error path: `GET /themes/default/secret.txt` returns 403 (extension not in whitelist).
- Error path: `GET /themes/default/manifest.webmanifest` returns 403 (`.webmanifest` deliberately excluded).
- Error path: `GET /themes/../etc/passwd` returns 403 (existing `..`-rejection).
- Error path: `themes/custom/tokens.css` symlink points to a file inside `RESULTS_ROOT` but outside `${RESULTS_ROOT}/branding/` (e.g., a results-level JSON); request returns 403 (tightened containment). **Covers AE5.**
- Error path: `themes/custom/tokens.css` symlink points to a file outside `RESULTS_ROOT` entirely (e.g., `/etc/passwd`); request returns 403.
- Edge case: `themes/custom/foo.css` is a symlink whose target file ends in `.txt`; suffix-check on `rel_str` accepts the request, containment-check on resolved target accepts the symlink only if it lands in `BRANDING_ROOT`. The request is served (operator-controlled symlink within their own branding dir is fine).

**Verification:**
- `pytest dashboard/test_serve.py` passes including new theme-allowlist cases.
- All existing test cases continue to pass (no regression in path-traversal or symlink-containment posture).

---

### U3. Create `dashboard/themes/default/`, extract assets, migrate rule bodies, update `index.html`

**Goal:** Land the directory contract: ship the current dashboard look as `themes/default/` and wire `index.html` to load it.

**Requirements:** R1, R4, R9, R13.

**Dependencies:** U1, U4. (U4 must serve `themes/` paths before this unit references them.)

**Files:**
- Create: `dashboard/themes/default/tokens.css`
- Create: `dashboard/themes/default/logo.svg`
- Create: `dashboard/themes/default/favicon.svg`
- Modify: `dashboard/style.css`
- Modify: `dashboard/index.html`

**Approach:**
- Move the entire `:root` block from `dashboard/style.css` to `dashboard/themes/default/tokens.css`. Leave `style.css` containing only structural rules.
- Migrate every rule body in `style.css` from legacy names (`--accent-indigo`, `--bg-primary`, etc.) to semantic names (`--color-brand-primary`, `--color-surface-base`, …). Drop the legacy names from `tokens.css` once migration is complete. Decorative `rgba(...)` literals stay untouched per R10 narrowing.
- Extract the inline SVG block from `dashboard/index.html` (lines 21-32) into `dashboard/themes/default/logo.svg`. Replace the SVG markup in `index.html` with `<img id="brandLogo" src="themes/default/logo.svg" alt="Rally OpenStack Monitor" width="32" height="32">`.
- Inline the existing gradient stops (`#6366f1` / `#06b6d4`) directly in `logo.svg` — `<img>`-loaded SVG does not inherit page CSS variables, so a custom theme that wants a different logo must supply its own `logo.svg`. Documented in CUSTOMIZING.md.
- Add explicit logo container sizing to `dashboard/style.css`:
  - `.logo { width: 32px; height: 32px; overflow: hidden; }` (in addition to existing flex/animation rules)
  - `#brandLogo { width: 32px; height: 32px; object-fit: contain; display: block; }`
  - This prevents a custom logo with unusual aspect ratio from breaking header layout.
- Derive a square `favicon.svg` from `logo.svg` (same artwork, simplified or trimmed for 16-32px viewport). Add `<link rel="icon" type="image/svg+xml" href="themes/default/favicon.svg" id="favicon">` to `<head>`.
- In `<head>`, place the theme `<link>` tags in the order shown in High-Level Technical Design: default tokens first, `style.css` second, custom tokens and overrides last with `onerror="this.remove()"`.
- Bump the `?v=...` version stamp to a new value to bust caches.

**Patterns to follow:** existing `<link rel="stylesheet" href="style.css?v=...">` pattern; the same query-string version-bust convention applies.

**Test scenarios:**
- Visual regression (manual): the dashboard renders byte-equivalent to `main` when no custom theme is mounted. Confirm by side-by-side screenshot comparison or `git stash` + reload. **Covers AE1.**
- Edge case: custom logo with extreme aspect ratio (e.g., 200x32) does not break header layout; `object-fit: contain` clips it to the 32x32 box.
- Edge case: deleting `dashboard/themes/default/logo.svg` shows a broken-image icon (acceptable — that's a developer error, not a runtime failure mode).

**Verification:**
- All existing CSS rules render correctly against the migrated semantic tokens (no missing variable warnings in browser DevTools).
- Logo and favicon load from `themes/default/`.
- Page title bar in browser shows the favicon.

---

### U5. Wire `scripts/entrypoint.sh` to symlink `themes/custom` into `${RESULTS_DIR}/branding/`

**Goal:** Establish the runtime symlink that makes operator-supplied themes visible to the dashboard server.

**Requirements:** R2, R7.

**Dependencies:** U3 (the `dashboard/themes/` directory must exist).

**Files:**
- Modify: `scripts/entrypoint.sh`

**Approach:**
- After the existing `mkdir -p "${RESULTS_DIR}"` block (line 49), add `mkdir -p "${RESULTS_DIR}/branding"`.
- After the existing `ln -sf` block for JSON files (lines 145-148), add `ln -sfn "${RESULTS_DIR}/branding" /dashboard/themes/custom`.
- Use `ln -sfn` (not `ln -sf`) for the directory case so re-execution overwrites a stale symlink instead of nesting one inside another.
- Log a single line confirming `Custom theme slot: ${RESULTS_DIR}/branding`.
- Failures abort startup per the script's existing `set -euo pipefail` posture (no special handling).

**Patterns to follow:** existing `mkdir -p "${RESULTS_DIR}"` at line 49 and `ln -sf` block at 145-148.

**Test scenarios:**
- Happy path: container starts, `dashboard/themes/custom` resolves to `${RESULTS_DIR}/branding` (symlink present, points at the right path). **Covers AE1.**
- Happy path: `${RESULTS_DIR}/branding/tokens.css` mounted via `-v` is reachable at `dashboard/themes/custom/tokens.css` after container start. **Covers AE2.**
- Edge case: re-running entrypoint (e.g., container restart) keeps the symlink correct rather than creating `themes/custom/branding` (validated by `ln -sfn` semantics).
- Edge case: `${RESULTS_DIR}/branding/` is empty; symlink still exists, dashboard handles 404s on theme assets gracefully (verified by U4 test coverage).

**Verification:**
- After container start, `docker exec rally-monitor ls -la /dashboard/themes/custom` shows a symlink to `/results/branding`.
- After `docker exec rally-monitor ls /results/branding`, the directory is empty by default.

---

### U6. Add custom-theme asset resolver in `dashboard/app.js`

**Goal:** At boot, swap `<img>` and `<link rel="icon">` to the custom theme's logo/favicon when present; fall through to default otherwise.

**Requirements:** R3, R6, R8.

**Dependencies:** U3, U4, U5.

**Files:**
- Modify: `dashboard/app.js`

**Approach:**
- Add a `resolveThemeAssets()` async function. For each `(elementId, attribute, customPath, defaultPath)` tuple in a small table, issue `fetch(customPath, { cache: 'no-store' })`. On `response.ok`, set the attribute to `customPath`. On any non-ok response or fetch error, leave at `defaultPath` (already set by `index.html`).
- GET (not HEAD) is used because `dashboard/serve.py` only implements `do_GET`. The probe payload is small (logo SVG, kilobytes); this runs once at boot, not per refresh.
- Call `resolveThemeAssets()` from a `DOMContentLoaded` handler. Run it once at boot, not on every refresh.
- Asset table: `[brandLogo, src, themes/custom/logo.svg, themes/default/logo.svg]`, `[favicon, href, themes/custom/favicon.svg, themes/default/favicon.svg]`.

**Patterns to follow:** existing async/await + try/catch error-swallowing in `fetchResults`, `fetchHistory`, etc.

**Test scenarios:**
- Happy path (no custom): both GET probes return 404; `brandLogo.src` ends with `themes/default/logo.svg`. Dashboard renders today's look. **Covers AE1.**
- Happy path (custom logo only): `themes/custom/logo.svg` returns 200; `brandLogo.src` ends with `themes/custom/logo.svg`. Favicon probe 404 → favicon stays at default. **Covers AE2 (partial overlay).**
- Edge case: GET fetch errors with network failure → element stays at default. **Covers AE4 (graceful degradation).**
- Integration: with U3's CSS cascade, a custom `tokens.css` overrides chart series colors AND a custom logo replaces the brand mark. Verified by manual smoke test in container. **Covers AE3.**

**Verification:**
- Console shows no JS errors on page load with or without custom theme.
- `<img id="brandLogo">` `src` attribute reflects the resolved path (inspect via DevTools).

---

### U7. Document the override contract

**Goal:** Make the contract discoverable so operators can use it without reading source.

**Requirements:** R5, R14.

**Dependencies:** U1, U3 (the contract names must be finalized).

**Files:**
- Create: `docs/CUSTOMIZING.md`
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Approach:**

- `docs/CUSTOMIZING.md` covers:
  - Bind-mount path (`${RESULTS_DIR}/branding/`).
  - Two operator workflows: (a) populate the existing `rally-results` volume's `branding/` subdir via `docker cp` or `docker exec`; (b) layer a nested bind mount: `-v ./my-theme:/results/branding:ro` in `docker/docker-compose.yml`. Show concrete examples for both.
  - Full public token contract (the narrow surface from U1, grouped by family).
  - File list operators may supply: `tokens.css`, `overrides.css`, `logo.svg`, `favicon.svg`. Each file is independently optional (partial overlay).
  - Minimum viable example: a five-line `tokens.css` overriding the brand color.
  - **What reskins:** every CSS rule that reads a public-contract token, plus Chart.js datasets and chart chrome.
  - **What does not reskin:** decorative alpha tints (status-chip backgrounds, glow shadows, glass borders, scrollbar overlays), the default `themes/default/logo.svg` gradient (because `<img>`-loaded SVG does not inherit page CSS variables — supply your own `logo.svg` to recolor the brand mark), gradients used only in default decoration. List concrete examples so operators know what to expect.
  - Upgrade-safety promise: contract names stable across versions; internal token names may rename without notice.
  - Roadmap pointer for items explicitly out of v1: theme picker, multiple presets, banner, page title overrides, light theme, hot reload.
- `README.md`: short "Customization" subsection pointing to `docs/CUSTOMIZING.md` and showing the one-line nested bind-mount example.
- `CLAUDE.md`: new "Theme Customization" subsection under Architecture documenting the `themes/<name>/` contract, the symlink topology, the cascade order in `index.html`, the `serve.py` allowlist branch (including the tightened `BRANDING_ROOT` containment), and the `<img>` SVG var-inheritance note.

**Test scenarios:**
- Test expectation: none — documentation only.

**Verification:**
- `docs/CUSTOMIZING.md` lists every public contract token from U1.
- A reader following only `docs/CUSTOMIZING.md` can produce a working custom theme that overrides the brand color.
- The "What does not reskin" section names at least three concrete examples so operators are not surprised.

---

## System-Wide Impact

- **Interaction graph:** `dashboard/index.html` `<head>` link order, `dashboard/style.css` `:root` consumers (every rule body), `dashboard/app.js` chart render and asset resolver, `dashboard/serve.py` allowlist, `scripts/entrypoint.sh` symlink topology.
- **Error propagation:** Missing custom theme files produce 404s in browser console (benign). Malformed `tokens.css` is parsed leniently by browsers (broken declarations dropped). GET-probe network errors during boot fall back to default. Container symlink failure aborts startup per `set -e` — surfaces misconfigurations early instead of producing a half-themed runtime.
- **State lifecycle risks:** Restarting the container recreates the symlink (`ln -sfn`); a stale symlink from a prior version is overwritten. Existing operators upgrading from current `main` see no behavior change because `${RESULTS_DIR}/branding/` is empty by default.
- **API surface parity:** Prometheus exporter (`exporter/rally_exporter.py`) is unaffected — it serves no UI. Rally task HTML reports (generated by `rally task report`) are out of scope; their styling stays Rally's default.
- **Integration coverage:** Cross-layer scenario — entrypoint creates branding dir + symlink, serve.py allowlist permits the path, app.js GET-probes resolve assets, charts read tokens from CSS cascade. Verified end-to-end via container smoke test.
- **Unchanged invariants:** `serve.py`'s strict-allowlist posture, path-traversal rejection, and `relative_to` containment check stay unchanged in semantics — extended to a new prefix with strictly tighter containment for the `/results/` half. CSP headers stay unchanged. Docker volume layout (`/results/`) is unchanged in shape; one new subdirectory (`branding/`) appears.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Token rename in `style.css` rule bodies (U3) misses a usage and a CSS rule silently breaks. | Manual visual smoke test on the default theme post-U3 lands; the AE1 invariant catches regressions. Browser DevTools "Computed" pane lists unresolved variables. |
| `<img>`-loaded SVG does not inherit page CSS variables, so a token-only custom theme cannot recolor the default logo gradient. | Explicitly documented in CUSTOMIZING.md: token overrides do not recolor `<img>` logos; supply a custom `logo.svg` to change the brand mark. Acknowledged tradeoff. |
| Custom logo with extreme aspect ratio breaks header layout. | U3 sets explicit `.logo` and `#brandLogo` width/height/object-fit/overflow rules. |
| Decorative alpha tints (status chip backgrounds, glow shadows) do not reskin under a custom theme — operator surprise. | Documented prominently in CUSTOMIZING.md "What does not reskin" with concrete examples. The price of "not fully customizable". |
| Operator mounts `${RESULTS_DIR}/branding/` containing a symlink to `/etc/passwd` and tries to read it via `themes/custom/etc-passwd`. | `serve.py`'s tightened containment requires resolved target to be under `${RESULTS_ROOT}/branding/`. Symlinks pointing outside that subdirectory return 403. Covered by AE5 test. |
| Operator mounts a symlink within `branding/` pointing to another file under `/results/` (e.g., a results JSON). | Same containment check rejects it. Strictly tighter than the existing JSON branch's whole-`/results/` permit. |
| Browser caches stale `tokens.css` after operator updates a custom theme. | Restart-to-see-changes is the supported activation path (out of scope: hot reload). The `?v=` query string in `index.html` bumps on default-theme changes; custom themes pay the cache cost on first load and can hard-reload as needed. |
| Future maintainer renames an internal token, breaking a custom theme that depends on it. | The U1 token contract is documented as the *only* stable surface; internal tokens may rename freely. CUSTOMIZING.md publishes the contract; `CLAUDE.md` reinforces it. |
| Nested bind mount (`-v ./theme:/results/branding`) overlays the named `rally-results` volume's `branding/` subdir, hiding any contents put there by the alternative workflow. | Documented in CUSTOMIZING.md: pick one workflow, do not mix them. |

---

## Documentation / Operational Notes

- `README.md` gets a new "Customization" subsection with a one-line nested bind-mount example.
- `CLAUDE.md` gets a new "Theme Customization" subsection.
- `docs/CUSTOMIZING.md` is new and is the contract source of truth.
- No CHANGELOG entry required at v1 (no migration; existing operators see no change). Consider adding one after the brainstorm artifact validates with users.
- Operational rollout: rebuild image, restart container. No data migration. Existing custom forks are unaffected because none are known to exist.

---

## Sources & References

- **Origin document:** `docs/brainstorms/2026-05-04-dashboard-theme-customization-requirements.md`
- **Ideation precursor:** `docs/ideation/2026-05-04-dashboard-customization-ideation.md`
- Related code: `dashboard/style.css`, `dashboard/app.js`, `dashboard/index.html`, `dashboard/serve.py`, `dashboard/test_serve.py`, `scripts/entrypoint.sh`, `docker/Dockerfile`, `docker/docker-compose.yml`.
- External docs: OpenStack Horizon themes — `docs.openstack.org/horizon/latest/configuration/themes.html`.
