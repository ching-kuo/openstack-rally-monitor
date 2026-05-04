---
date: 2026-05-04
topic: dashboard-theme-customization
---

# Dashboard Theme Customization

## Summary

Adopt the portable subset of OpenStack Horizon's theming model on the Rally Monitor dashboard: ship the current look as a baked-in `default` theme, expose an optional `custom/` slot operators can bind-mount to override colorways, logo, and favicon — and refactor `:root` and `app.js` so a custom theme reskins the Chart.js charts coherently alongside the HTML and CSS.

---

## Problem Frame

cn.tug operators run the Rally Monitor against multiple OpenStack environments. Every instance currently ships an identical fixed indigo/cyan dark theme with a hardcoded inline-SVG logo and no favicon. The dashboard's colors live as `:root` CSS custom properties in `dashboard/style.css` and would already be a usable theming surface — but Chart.js colors and service-icon palettes are hardcoded in `dashboard/app.js`, so any color change leaves charts visually orphaned. There is no documented, upgrade-safe override path; an operator who wants to brand the dashboard for a screenshot, demo, or customer-facing view today has to fork the repo or edit files inside the running container and risk every upgrade undoing them.

The cost is small per deployment but compounds across the lab footprint: identical-looking dashboards are an invitation for wrong-cluster mistakes during incidents, and the absence of a stable contract means even small theme changes have to be argued from scratch each time. Operators familiar with OpenStack Horizon expect a `themes/<name>/` directory contract to exist, and reach for it first when asked to customize.

---

## Actors

- A1. **Operator (deploying the container):** Sets env vars in `.env`, optionally bind-mounts a host directory into the rally-results volume to provide custom theme files, restarts the container.
- A2. **Operator (using the dashboard in a browser):** Opens the dashboard, sees either the default look or the bind-mounted custom look. No in-UI theme picker.
- A3. **Maintainer (this repo):** Owns the default theme, the documented token contract, and the `themes/` directory shape. Refactors that rename internal tokens must keep the contract surface stable.

---

## Requirements

**Theme directory contract**
- R1. The dashboard ships with a `dashboard/themes/default/` directory that holds the current look — token definitions, optional style overrides, logo, and favicon. Tokens are CSS custom properties (no SCSS, no build step).
- R2. A `dashboard/themes/custom/` slot exists as the operator-supplied override target. The slot is empty in the shipped image; when populated, its files take precedence over the corresponding default files.
- R3. A custom theme may supply any subset of the override surface — partial overlay. Files the custom theme does not provide fall through to the default theme transparently.
- R4. The override surface is exactly: token definitions, an optional extra stylesheet for rules that aren't expressible as token overrides, the logo, and the favicon. Nothing else (no page title, no copy, no service list, no banners).
- R5. The maintainer-owned token contract is documented and stable — internal token names may be refactored freely, but the documented contract names are part of the public surface and must not break across upgrades without notice.

**Override activation**
- R6. Activation is auto-detect, not env-var-gated: if the `custom/` slot contains the recognized override files, the dashboard applies them. If the slot is empty (the default), the dashboard renders the default theme exactly as today.
- R7. The bind-mount target lives under the existing rally-results Docker volume. The container surface that maps the host-mounted directory into `dashboard/themes/custom/` is established at container startup by the existing entrypoint, reusing the symlink-into-`/results/` pattern already used for `results.json` and friends.
- R8. A broken or partially populated custom theme degrades gracefully: missing files fall through to the default; a malformed token file does not break the dashboard load (the dashboard still renders, even if the result looks wrong). No fail-closed behavior at the container boundary in v1.

**Coherent reskin (charts + HTML + CSS)**
- R9. `:root` in `dashboard/style.css` is refactored to a small set of semantic token names that the contract documents (e.g., names denoting role: success, failure, pending, chart series, surface, text). The current hex values map to those new names so the default look is unchanged.
- R10. `dashboard/app.js` is refactored so Chart.js dataset colors and the service-icon palette are read from the same semantic tokens at chart-render time — no hardcoded hex outside `:root`.
- R11. When a custom theme overrides token values, the next time charts render they pick up the new colors. Mid-render swap during a single chart instance is not required (a normal page or chart refresh is enough).

**Serving and security**
- R12. `dashboard/serve.py`'s static-file allowlist is extended to permit serving files under the `themes/` subtree. Existing path-traversal rejection and the explicit allowlist posture must remain intact.
- R13. The MIME types for `.css` and `.svg` (and `.ico` if used) are served correctly for files under the `themes/` subtree; no inline `<style>` injection is added to `index.html`.

**Documentation**
- R14. `README.md` and `CLAUDE.md` document: the override contract (which files, which token names), how to bind-mount a custom theme, the expected fallback behavior, and the minimum viable custom theme (e.g., a single token-overrides file).

---

## Acceptance Examples

- AE1. **Covers R6, R7.** Given a freshly built image with no host-mounted custom theme, when an operator starts the container and opens the dashboard, the dashboard renders the same look it does today (default theme, indigo/cyan dark, current inline-SVG logo).
- AE2. **Covers R3, R6.** Given a host-mounted custom theme that supplies only a token-overrides file (no logo, no favicon, no extra stylesheet), when the operator restarts the container and opens the dashboard, the colors reflect the operator's overrides while the logo and favicon remain the defaults.
- AE3. **Covers R10, R11.** Given a custom theme that changes the success and failure tokens to brand-specific colors, when the dashboard refreshes charts after a Rally run, the bars and status indicators on the charts use the brand colors — not the shipped indigo/emerald.
- AE4. **Covers R8.** Given a custom theme containing a syntactically broken token file, when the operator opens the dashboard, the dashboard still loads and renders (the result may look mis-themed, but the page is not blank and JS errors do not abort the bootstrap).
- AE5. **Covers R12.** Given a request for a path under the `themes/` subtree that escapes the directory (path traversal attempt), when `serve.py` handles it, the request is rejected as today.

---

## Success Criteria

- An operator who has never seen this codebase can rebrand the dashboard's colors, logo, and favicon by dropping 1-4 files into a host-mounted directory and restarting the container — without editing any tracked file in the repo and without rebuilding the image.
- The default-theme experience is byte-equivalent to today's dashboard: existing operators see no visual change after the refactor.
- A custom theme that overrides only the color tokens reskins the entire visible surface, including Chart.js bars, gauges, and status badges. No "indigo bars on an orange background" half-themed result.
- The token contract is documented well enough that a reviewer can tell which names a custom theme is allowed to depend on and which names are internal.
- `ce-plan` can produce a step-by-step implementation plan from this document without inventing product behavior, override semantics, or scope boundaries.

---

## Scope Boundaries

- Multi-preset theme library (e.g., `high-contrast`, `openstack-native`, `prod-red`, `staging-amber`) — only `default` and an optional `custom/` slot ship in v1.
- In-dashboard theme picker UI (header dropdown, user menu, settings page).
- Per-user theme persistence (`localStorage`, cookies).
- Env-var theme registry (Horizon's `AVAILABLE_THEMES` / `DEFAULT_THEME` / `SELECTABLE_THEMES` analogues) — the single auto-detected slot doesn't need a registry.
- Customizing page title, header copy, service list, embedded links, or any non-visual text.
- Environment-disambiguation banner ("PROD" / "STAGING" stripe). Recognized as valuable and tracked separately; out of this brainstorm.
- Single-accent-env-var palette derivation (HSL math, contrast guard, auto-favicon generation from logo).
- Web App Manifest / PWA install support and `theme-color` meta integration.
- Hot reload of theme files without container restart.
- Light theme generation, automatic dark/light pairs, `prefers-color-scheme` switching.
- Server-side theme compilation, SCSS, or any build step.
- WCAG contrast validation at startup or fail-closed behavior on broken custom themes.
- Migration tooling for operators who have already forked the dashboard or inlined custom CSS into a child image (no such operators are known to exist).

---

## Key Decisions

- **Adopt Horizon's directory shape, drop Horizon's compilation tax:** Operators reach for the Horizon mental model first; honoring the directory contract (`themes/<name>/` with token file, optional overrides, assets folder) gives free familiarity. Stripping SCSS, Django templates, `COMPRESS_OFFLINE`, and the cookie picker preserves the no-build-step posture this dashboard already has.
- **Auto-detect activation, not env-var gate:** The codebase already uses presence-of-files-under-`/results/` as a runtime-injection signal (`results.json`, `history.json`). Reusing the same signal for theme override avoids introducing a new configuration surface and keeps the override path consistent with how data already flows in.
- **Partial overlay, not full replacement:** A custom theme that wants only to swap two colors should not have to copy the rest of the default theme. Falling through to default for absent files makes future additions to the default theme forward-compatible without breaking existing custom themes.
- **Charts in scope, not deferred:** A theme that changes colors but leaves the charts indigo is a worse outcome than no theming at all — it signals a broken feature. Refactoring `:root` semantics and `app.js` chart-color reads is the minimum viable end-state.
- **No fail-closed on broken custom themes in v1:** This is an internal operator tool with ~10 users; a malformed file should not nuke the dashboard, but a startup validator with WCAG contrast checks is over-investment for the current audience. Revisit if the audience grows or if multi-tenant white-labeling becomes a goal.
- **Bind-mount under the existing rally-results volume, not a new top-level volume:** Reuses the trust boundary `serve.py` already accepts, avoids changing operator deployment shape (no new `-v` argument required if they're already mounting the results volume).

---

## Dependencies / Assumptions

- The current `:root` CSS custom properties in `dashboard/style.css` cover enough of the visible color surface that adding semantic-named tokens on top is sufficient — no rewrite of style rules is needed.
- The Chart.js usage in `dashboard/app.js` is amenable to reading colors via `getComputedStyle` at render time; no exotic Chart.js plugin or custom renderer blocks this.
- The existing `entrypoint.sh` symlink/`/results/`-write pattern is the right place to land the `themes/custom/` symlink — confirmed by the ideation grounding scan but not re-verified here; planning should confirm.
- No existing operator has forked the dashboard or inlined custom CSS into a child image. If any have, the token-rename refactor is a breaking change for them.
- `dashboard/serve.py`'s allowlist mechanism can be extended with a directory-prefix entry without rewriting the request handler. Verified at the ideation level; planning should confirm the exact extension point.

---

## Outstanding Questions

### Deferred to Planning

- [Affects R9, R10][Technical] Exact set of semantic token names in the contract — which roles to surface (success, failure, pending, surface-1, surface-2, text-primary, text-muted, accent, chart-series-1..n, etc.). The shape is brainstormed; the exact list is a planning decision.
- [Affects R7, R12][Technical] Whether the `themes/custom/` symlink target is a dedicated `/results/branding/` subdirectory or some other path under `/results/`, and how `serve.py`'s allowlist accommodates a symlinked subtree under a static-allowlisted directory.
- [Affects R10][Needs research] Whether the existing chart instances need to be destroyed and recreated to pick up new token values, or if `chart.update()` is enough — depends on how Chart.js caches resolved colors.
- [Affects R8][Technical] How exactly to make a malformed `tokens.css` degrade gracefully — browser CSS parsers ignore broken declarations by default, but if the file 404s or returns an HTTP error, the fallback path needs explicit handling.
- [Affects R14][Documentation] Whether to ship an example `themes/custom.example/` directory in-repo as a starter, or to keep the slot strictly empty and document the contract in prose.
