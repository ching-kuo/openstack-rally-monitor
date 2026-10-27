"""Run snippets against the dashboard's JavaScript under node.

The ES modules (dashboard/js/) touch the DOM only when a render function is
called, never at import time (main.js, the entry point, is the one exception
and is not imported here), so a test imports exactly the functions it needs.
Render functions run against FAKE_DOM, a minimal stand-in for `document`.

report.js is a classic script that calls initReport() at load time, so it
cannot be loaded under node; extract_function() pulls a self-contained
function out of it by source and run_script() evaluates it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS_DIR = Path(__file__).resolve().parent / "js"
NODE = shutil.which("node")

# Locale-pinned environment for tests of formatted times.
UTC_EN = {"TZ": "UTC", "LANG": "en_US.UTF-8", "PATH": "/usr/bin:/bin"}
# A promise that never settles, for stalled-fetch stubs.
NEVER = "new Promise(() => {})"

# Without node the suites skip rather than fail.
requires_node = pytest.mark.skipif(NODE is None, reason="node not available")

# Just enough of the DOM for the render functions: elements keep their
# children, attributes, listeners and class list; getElementById creates an
# element on first use, so a test reads it back through `els`.
FAKE_DOM = r"""
class FakeClassList {
  constructor(el) { this.el = el; }
  _all() { return this.el.className.split(/\s+/).filter(Boolean); }
  contains(c) { return this._all().includes(c); }
  add(c) { if (!this.contains(c)) this.el.className = [...this._all(), c].join(" "); }
  remove(c) { this.el.className = this._all().filter((x) => x !== c).join(" "); }
  toggle(c, on) { (on ?? !this.contains(c)) ? this.add(c) : this.remove(c); }
}
class FakeElement {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.className = ""; this.innerHTML = "";
    this.textContent = ""; this.attrs = {}; this.listeners = {}; this.hidden = false;
    this.dataset = {}; this.style = {}; this.title = ""; this.tabIndex = -1;
    this.classList = new FakeClassList(this);
  }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  append(...cs) { cs.forEach((c) => this.appendChild(c)); }
  replaceChildren(...cs) {
    this.children.forEach((c) => { c.parent = null; });
    this.children = [];
    this.append(...cs);
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event = {}) {
    for (const fn of this.listeners[type] || []) fn({ preventDefault() {}, ...event });
  }
  querySelector(sel) { return ((this._q ||= {})[sel] ||= new FakeElement("span")); }
  // Only a single ".class" selector matches (descendants, document order);
  // anything else finds nothing.
  querySelectorAll(sel) {
    if (!/^\.[\w-]+$/.test(sel)) return [];
    const cls = sel.slice(1);
    const walk = (el) => (el.children || []).flatMap((c) =>
      [...(c.classList && c.classList.contains(cls) ? [c] : []), ...walk(c)]);
    return walk(this);
  }
  // Elements from getElementById are in the page; others only once appended
  // under one (replaceChildren disconnects the old children).
  get parentElement() { return this.parent || this; }
  get isConnected() { return Boolean(this.inPage || (this.parent && this.parent.isConnected)); }
  contains(el) { return el === this || this.children.some((c) => c.contains && c.contains(el)); }
  focus() { document.activeElement = this; }
  getContext() { return {}; }
}
const els = {};
const docListeners = {};
globalThis.document = {
  getElementById: (id) => (els[id] ||= Object.assign(new FakeElement("div"), { inPage: true })),
  createElement: (tag) => new FakeElement(tag),
  addEventListener: (type, fn) => { (docListeners[type] ||= []).push(fn); },
  querySelectorAll: () => [],
  activeElement: null,
  hidden: false,
  documentElement: {},
};
const replacedUrls = [];
globalThis.window = {
  location: { hash: "", pathname: "/", search: "" },
  history: { replaceState: (_s, _t, url) => replacedUrls.push(url) },
  addEventListener: () => {},
};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "#123456" });
const chartConfigs = [];
globalThis.Chart = class {
  constructor(_ctx, config) { this.config = config; chartConfigs.push(config); }
  destroy() {}
};
"""


def imports(module: str, *names: str) -> str:
    """`import { a, b } from "<dashboard/js/module.js>";` as an absolute URL."""
    url = (JS_DIR / f"{module}.js").as_uri()
    return f"import {{ {', '.join(names)} }} from {url!r};\n"


def _run_node(args: list[str], env: dict[str, str] | None) -> str:
    result = subprocess.run(
        [NODE, *args], capture_output=True, text=True, check=False, env=env,
    )
    assert result.returncode == 0, f"node failed:\n{result.stderr}"
    return result.stdout.strip()


def run_module(snippet: str, env: dict[str, str] | None = None) -> str:
    """Run `snippet` as an ES module and return its stripped stdout. `env`
    (e.g. TZ, LANG) replaces the environment, for locale-dependent output."""
    return _run_node(["--input-type=module", "-e", snippet], env)


def run_script(snippet: str) -> str:
    """Run `snippet` as a classic script and return its stripped stdout."""
    return _run_node(["-e", snippet], None)


def assert_cases(cases: list[tuple[str, object]], out: list) -> None:
    """Compare a batched node run with its case table. `cases` holds
    (case id, expected) pairs in the order the snippet evaluated them; a
    mismatch names the failing case ids rather than list positions."""
    assert len(out) == len(cases), f"{len(cases)} cases but {len(out)} results"
    assert dict(zip([case_id for case_id, _ in cases], out)) == dict(cases)


def extract_function(path: Path, name: str) -> str:
    """The source of `function name(...) {...}` in `path`, by brace matching
    (the function must not contain braces inside strings or comments)."""
    src = path.read_text()
    start = src.index(f"function {name}(")
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"could not brace-match {name}")


def run_json(snippet: str, env: dict[str, str] | None = None):
    """run_module for a snippet that writes one JSON document to stdout."""
    return json.loads(run_module(snippet, env))
