---
date: 2026-05-04
topic: dashboard-customization
focus: Let users customize the dashboard look (colorways, logo, favicon) — "not fully customizable", probably tokens.css. Refined toward Horizon-style theming after follow-up.
mode: repo-grounded
---

# Ideation: Dashboard Customization

## Grounding Context

### Codebase Context
- Pure static HTML/JS/CSS dashboard at `dashboard/` — no build step. Served by Python `http.server` via `dashboard/serve.py` (strict ALLOWED_STATIC + ALLOWED_JSON_SYMLINKS allowlist; rejects path traversal).
- All colors already defined as CSS custom properties in `:root` in `dashboard/style.css` (lines 24-64). Logo is an inline SVG with a hardcoded indigo→cyan gradient in `dashboard/index.html`. No favicon defined.
- Chart.js colors and service-icon palettes are hardcoded inside `dashboard/app.js` (lines 462-470, 541-549) — would need to be wired through tokens for any theme change to fully reskin charts.
- Existing runtime-injection precedent: `dashboard/results.json` etc. are symlinks from `/dashboard/` into the `/results/` Docker volume; `entrypoint.sh` already writes `/rally/rally_env` from environment at container start.
- Internal/operator audience (cn.tug labs running OpenStack clouds). No prior tickets requesting customization. No `docs/solutions/` directory exists yet.

### External Context
- **OpenStack Horizon theming model** (the user's referenced precedent): `openstack_dashboard/themes/<name>/` directories with `_variables.scss`, `_styles.scss`, optional `static/img/` (logo, favicon, splash) and `templates/` overrides. Configured via `AVAILABLE_THEMES`, `DEFAULT_THEME`, `SELECTABLE_THEMES` settings. User-visible cookie-keyed picker in the user menu when >1 selectable theme exists. Known production pitfall: requires `COMPRESS_OFFLINE = True` and SCSS compilation at deploy time. Sources: docs.openstack.org/horizon/latest/configuration/themes.html, github.com/osism/openstack-themes, github.com/openstack/charm-openstack-dashboard.
- **Three-tier design tokens** (primitive → semantic → component) is the industry-standard runtime theming pattern; for small tools 2 tiers suffice (Tailwind, Material 3, GitHub Primer, Adobe Spectrum).
- **`envsubst` at container entrypoint** is the canonical static-dashboard runtime-config pattern; substitution must be scoped to declared vars to avoid clobbering literal `$` in CSS.
- **Argo CD `ui.cssurl` + volume-mount /shared/app/custom/** is the structural precedent for "drop a CSS file in a known mount path."
- **Vaultwarden 2025.7 regression** is the cautionary tale against file-swap as a contract: upstream inlined SVGs and broke every customer's logo override.
- Pitfalls: FOUC if tokens load late (mitigation: external CSS loaded render-blocking before body), CSP issues with inline `<style>` (use external file), cache staleness without content-hashed filenames (use `?v=` query string written by entrypoint), WCAG 4.5:1 contrast risk with arbitrary user colors.

## Ranked Ideas

### 1. Horizon-Style Theme Directories with Env Registry and Optional Picker
**Description:** Adopt Horizon's portable theming subset, stripped of its build-time machinery. Directory layout under `dashboard/themes/<name>/`:

```
themes/
  default/
    tokens.css            # CSS custom properties (the _variables.scss equivalent)
    overrides.css         # optional extra rules (the _styles.scss equivalent)
    assets/
      logo.svg
      favicon.svg
    metadata.json         # { "name": "default", "label": "Default Dark" }
  openstack/              # OpenStack-native red/dark default
  high-contrast/
  prod-red/
  staging-amber/
  custom/                 # bind-mount target for operator-supplied themes
```

Configuration mirrors Horizon's settings, exposed as env vars consumed by `entrypoint.sh`:
- `DASHBOARD_AVAILABLE_THEMES=default,openstack,high-contrast,custom`
- `DASHBOARD_DEFAULT_THEME=default`
- `DASHBOARD_SELECTABLE_THEMES=default,openstack,high-contrast` (omits `custom` from picker; private base themes pattern)

`entrypoint.sh` writes `/results/dashboard_config.json` from these env vars (composes with the single-config-mechanism survivor below) and ensures `dashboard/themes/custom/` is a symlink into `/results/branding/` so operators bind-mount once and edit live. `app.js` reads the config on load, applies the active theme by:
1. Setting `<link id="theme-tokens" href="themes/<name>/tokens.css">` (sets CSS custom properties)
2. Optionally appending `<link id="theme-overrides" href="themes/<name>/overrides.css">` if present
3. Swapping the logo `<img src>` to `themes/<name>/assets/logo.svg` (with fallback to inline SVG default if 404)
4. Setting the favicon `<link rel="icon">` to `themes/<name>/assets/favicon.svg`

Persists the user's selection in `localStorage` (modern equivalent of Horizon's cookie). Renders a picker dropdown in the header when `DASHBOARD_SELECTABLE_THEMES` lists more than one theme; single-tuple disables the picker (matching Horizon's behavior). Single allowlist addition in `serve.py`: `themes/` prefix.

**Warrant:** `external:` Horizon's themes/ + AVAILABLE_THEMES + cookie-keyed picker is documented at docs.openstack.org/horizon/latest/configuration/themes.html and battle-tested in OSISM, STFC, and the Juju charm. `direct:` `dashboard/style.css` already centralizes colors as `:root` custom properties — the substrate the `_variables.scss` analogue needs is already there. `reasoned:` cn.tug operators already know Horizon's theming UX, so a familiar contract has near-zero cognitive cost; the portable subset (CSS custom properties for variables, plain CSS for overrides, static assets folder) drops Horizon's SCSS compilation tax which is overkill here.
**Rationale:** This is the user's stated reference, mapped to the constraints of a no-build static dashboard. Composes the strongest pieces of the other survivors (curated presets, override directory, manifest-style assets) under a single contract operators already know.
**Downsides:** More machinery than a single-env-var solution — a theme is now a directory with multiple files. Picker UI is incremental work (~30 lines of JS). Charts only reskin if Chart.js reads tokens at render time (idea #2 must land first or alongside).
**Confidence:** 85%
**Complexity:** Medium
**Status:** Unexplored

### 2. Tokens-as-Data Foundation (tiered semantic tokens shared by CSS and Chart.js)
**Description:** Refactor `dashboard/style.css` `:root` from flat hex values into a 2-tier token system: primitives (`--blue-500`, `--slate-900`) and semantics (`--color-success`, `--color-failure-bg`, `--chart-pass`, `--chart-fail`). Refactor `app.js` so Chart.js colors and service-icon palettes are read at render time via `getComputedStyle(document.documentElement).getPropertyValue('--chart-pass')` — no hardcoded hex anywhere outside `:root`. With idea #1, every theme's `tokens.css` only needs to redefine semantic names, and the entire system reskins coherently.
**Warrant:** `direct:` `dashboard/app.js` currently hardcodes Chart.js colors at lines 462-470, 541-549; the duplication is real and visible. `external:` Tailwind, Material 3, GitHub Primer, Adobe Spectrum all converged on tiered design tokens; Grafana panel theming uses the exact `getComputedStyle` pattern.
**Rationale:** Foundation that every other customization idea rests on. Without it, a partly-themed dashboard (indigo bars on an orange-branded layout) is the worst result — strictly worse than the unthemed default because it signals broken customization.
**Downsides:** Largest implementation cost; touches three files; needs a re-test of every chart.
**Confidence:** 85%
**Complexity:** Medium
**Status:** Unexplored

### 3. Single config.json via entrypoint + /results/ Symlink (the unifying mechanism)
**Description:** One runtime config file as the transport for any customization. `entrypoint.sh` reads env vars (`DASHBOARD_TITLE`, `DASHBOARD_DEFAULT_THEME`, `DASHBOARD_AVAILABLE_THEMES`, `DASHBOARD_SELECTABLE_THEMES`, `RALLY_ENV_LABEL`, etc.) and any bind-mounted `/results/branding/config.json`, merges them, and writes `/results/dashboard_config.json` — symlinked into `/dashboard/` identically to `results.json`. `app.js` fetches it on load and applies the active theme, env banner, page title, etc. Cache-busting via a version stamp written by entrypoint into the link tag.
**Warrant:** `direct:` `dashboard/results.json`, `history.json`, `health.json`, `health_history.json` are *already* symlinks from `/dashboard/` into `/results/`; `entrypoint.sh` already writes `/rally/rally_env` from env. Both halves of this mechanism exist. `external:` Argo CD `ui.cssurl` + ConfigMap and OpenSearch Dashboards' `opensearch_dashboards.yml` + volume-mount adopt this exact "single config object served at known URL" shape.
**Rationale:** The "how" that makes idea #1 ship cheaply. Every future "can we make X configurable?" request lands in the same file with the same mechanism. Pure leverage on existing patterns.
**Downsides:** JS-applied tokens at load time has a FOUC window unless the script tag is render-blocking in `<head>`; needs care. Tempts scope creep — must keep a stable contract subset documented.
**Confidence:** 80%
**Complexity:** Medium
**Status:** Unexplored

### 4. /results/branding/ Override Directory + Web App Manifest
**Description:** Reserve `/results/branding/` as a host-mountable asset directory; `entrypoint.sh` symlinks it into `dashboard/themes/custom/` (composes with idea #1). Operators drop `tokens.css`, `overrides.css`, `assets/logo.svg`, `assets/favicon.svg`, plus `manifest.webmanifest` (W3C standard — free PWA install, mobile home-screen icons, browser theme-color UI tinting). Zero changes to `serve.py`'s ALLOWED_STATIC because `/results/` symlink trust boundary already exists; one new prefix entry for `themes/`.
**Warrant:** `direct:` `dashboard/results.json` is already a `/results/` symlink — trust boundary, mount point, and serve.py allowlist all already accept this shape. `external:` Argo CD's `/shared/app/custom/` mount; W3C Web App Manifest is browser-native.
**Rationale:** White-labeling without rebuilding the image. Adopts a web standard that compounds — favicon, logo, install prompt, mobile icons, OS-level theme-color all from one file.
**Downsides:** Fewer guardrails than #5 — operators can mount unreadable themes. May need a MIME-type entry for `.webmanifest` in serve.py.
**Confidence:** 75%
**Complexity:** Low-Medium
**Status:** Unexplored

### 5. Single Accent Env Var → Derived Palette (with auto-favicon, contrast guard, optional auto-light)
**Description:** For operators who want one knob, not a directory: `DASHBOARD_ACCENT=#hexcolor` (and optionally `DASHBOARD_LOGO_URL`). `entrypoint.sh` runs a small Python step that derives a coherent palette via HSL math (primary, hover, gradients, focus rings, chart colors), validates WCAG 4.5:1 contrast against fixed text/background, generates favicon.svg from the logo (or a colored monogram from accent + project name), and writes a generated theme into `dashboard/themes/auto/tokens.css` so it composes with idea #1. Optional `DASHBOARD_LIGHT=true` derives light theme via L-channel inversion.
**Warrant:** `direct:` `entrypoint.sh` already writes `/rally/rally_env` from env vars; reusing the pattern is free. `external:` Tailwind/M3 tonal palettes derive coherent sets from a single hue; Grafana Enterprise `GF_WHITE_LABELING_*` uses single-string env contracts; WCAG 4.5:1 is the industry contrast threshold.
**Rationale:** Smallest non-zero customization surface — one hex code — exactly matches "not fully customizable". Contrast fail-closed mirrors the repo's existing `auto_purge_rgw` posture. For deployments that don't want to manage a theme directory.
**Downsides:** Derived palettes can look off with off-brand hues; HSL math has edge cases at extremes. Adds a Python derivation step at entrypoint.
**Confidence:** 70%
**Complexity:** Medium
**Status:** Unexplored

### 6. Environment-Disambiguation Banner (orthogonal addition)
**Description:** Single `RALLY_ENV_LABEL` env var (e.g., `prod-east`, `staging-tw`, `lab-dev`) auto-paints a top banner stripe, an accent color, and a stamped favicon (canvas-generated colored square + 2-letter monogram). Hash-derived hue keys to label by default; explicit `RALLY_ENV_COLOR` overrides. Independent of any theming work — composes with all other survivors.
**Warrant:** `direct:` cn.tug labs run multiple OpenStack clouds and each rally-monitor instance currently looks identical; no in-repo signal disambiguates. `external:` AWS console env-color browser extensions, GitHub Enterprise environment ribbons, GitHub/GitLab identicons all exist for this.
**Rationale:** Solves the actual on-call pain (wrong-cluster mistakes), not the cosmetic version. Cheap to ship and probably the highest-ROI piece even if full theming is deferred.
**Downsides:** Doesn't satisfy operators who want a custom logo for screenshots/demos. Risks narrowing scope before demand is validated.
**Confidence:** 70%
**Complexity:** Low
**Status:** Unexplored

---

## Recommended Composition

For a Horizon-style outcome that fits this codebase:

- **Ship together:** #1 (theme directories + picker) + #2 (token tiering for charts) + #3 (config.json mechanism). This is the foundation Horizon-style theming requires here, minus the SCSS overhead.
- **Compose orthogonally:** #4 (custom/ directory bind-mount) and #6 (env banner) layer on top with no friction.
- **Defer or skip:** #5 (single-accent derivation) is a power-user nicety; ship the directory model first and add this only if operators ask for it.

## Rejection Summary

Full critique of all 48 raw candidates is preserved at `/tmp/compound-engineering/ce-ideate/d4a7b2e9/`. Main rejection patterns:

| Category | Examples | Reason |
|---|---|---|
| Below ambition / overengineering for ~10-user internal tool | `/api/customization` introspection endpoint, hidden `/admin/theme` GUI, MSP multi-tenant with Host header routing, hot-reloading via inotify | No demand evidence; permanent maintenance surface for cosmetic feature |
| Subject-replacement | Fork-per-customer bind-mount; "don't build it / ask who asked" | Rejects the feature rather than operating on it |
| Scope expansion beyond "not fully customizable" | Branding-incomplete-without-copy-and-service-list, header/footer template partials | User explicitly bounded the scope |
| Tactical details folded into survivors | Cache-busting query string, external-CSS-only contract, logo-as-Data-URL, auto-favicon derivation | Implementation detail of larger survivor |
| Duplicates | Multiple framings of "one accent env var", "five named presets", "drop-a-file override directory", "environment banner" | Strongest framing kept; weaker ones dropped |
| Feature creep unrelated to look | NOC TV wall mode, per-operator localStorage, mandatory boot gate | Different feature; not customization |

| # | Idea | Reason |
|---|---|---|
| F1#1 | Token-name stability contract | Subsumed by #1 + #3 |
| F1#3 | Cache-busting query string | Tactical detail folded into #3 |
| F1#4 | Logo-by-URL contract | Subsumed by #1 + #4 |
| F1#6 | /api/customization endpoint | Below ambition floor for ~10-user tool |
| F1#7 | External-CSS-only contract | Tactical; folded into #1 |
| F2#2 | Auto-derive favicon from logo | Implementation detail of #5 |
| F2#4 | Just DASHBOARD_TITLE | Below user's stated ask |
| F2#7 | Logo-as-Data-URL env var | Subsumed by #1/#4 |
| F2#8 | Banner-only customization | Subsumed by #6 |
| F3#2 | Fork-per-customer | Subject-replacement |
| F3#3 | Inline envsubst in index.html | Single-file blob; #3 cleaner |
| F3#4 | Branding-incomplete-without-copy | Scope expansion |
| F3#5 | Don't build it / ask who asked | Belongs in brainstorm, not survivor |
| F3#6, F6#2 | MSP multi-tenant | Too much for ~10-user tool |
| F3#7 | Auto-branding hash-derived | Folded into #6 |
| F3#8 | CUSTOMIZING.md docs only | Below meeting-test floor |
| F5#1 | mplstyle flat key:value | Tactical format; no edge over CSS |
| F5#4 | ICC profile cross-surface | Speculative; no demand for Rally HTML report rebrand |
| F5#5 | PowerPoint master slides | Scope expansion |
| F5#6 | Tailwind theme.extend | Folded into #3 merge logic |
| F5#7 | Hot-swap mtime watch | Feature creep |
| F5#8 | iOS shortcut external URL | Remote-fetch attack surface for negligible gain |
| F6#1 | Mandatory boot gate | Breaks existing deployments |
| F6#3 | Hot-reloading theme tokens | Feature creep |
| F6#5 | NOC TV wall mode | Separate feature |
| F6#6 | Per-operator localStorage | Layered atop survivor; not core |
| F6#7 | Hidden /admin/theme editor | Overengineering; adds POST surface |
| F6#8 | Adopt OpenStack brand by default | Folded into #1 as the `openstack` preset |
