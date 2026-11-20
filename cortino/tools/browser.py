"""Browser interface for the visual and functional checks, and its Playwright implementation."""

import json
from pathlib import Path
from typing import Protocol

Box = tuple[float, float, float, float]
TIMEOUT_MS = 15_000

# Each chart's box as fractions of its page element: (x, y, w, h).
_BOXES_JS = """() => {
  const out = {};
  for (const e of document.querySelectorAll('[data-chart-id]')) {
    const p = e.closest('[data-page]').getBoundingClientRect(), r = e.getBoundingClientRect();
    out[e.dataset.chartId] = [(r.x - p.x) / p.width, (r.y - p.y) / p.height, r.width / p.width, r.height / p.height];
  }
  return out;
}"""


class BrowserError(Exception):
    pass


class Browser(Protocol):
    def screenshot(self, url: str, path: Path) -> None: ...

    def boxes(self, url: str) -> dict[str, Box]: ...

    def set_filter(self, url: str, filter_id: str, value: str) -> None: ...

    def read_chart_data(self, url: str, chart_id: str) -> list[dict]: ...


class PlaywrightBrowser:
    """Headless Chromium with one page. Each call loads `url` unless it was the url of the last
    load (compared as given, since the page reports a normalised url), so set_filter and
    read_chart_data on the same url see the same filter state. Call close()."""

    def __init__(self, width: int = 1400, height: int = 1000):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch()
            self._page = self._browser.new_page(viewport={"width": width, "height": height})
        except Exception:
            self._pw.stop()
            raise
        self._page.set_default_timeout(TIMEOUT_MS)
        self._page.set_default_navigation_timeout(TIMEOUT_MS)
        self._url: str | None = None

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()

    def _run(self, url: str, action):
        from playwright.sync_api import Error

        try:
            if self._url != url:
                self._url = None
                self._page.goto(url)
                self._page.wait_for_function("() => window.cortino && window.cortino.ready")
                self._url = url
            self._page.evaluate("() => window.cortino.ready")
            return action(self._page)
        except Error as e:
            raise BrowserError(str(e).splitlines()[0]) from e

    def screenshot(self, url: str, path: Path) -> None:
        self._run(url, lambda page: page.screenshot(path=str(path), full_page=True))

    def boxes(self, url: str) -> dict[str, Box]:
        return {k: tuple(v) for k, v in self._run(url, lambda page: page.evaluate(_BOXES_JS)).items()}

    def set_filter(self, url: str, filter_id: str, value: str) -> None:
        def act(page):
            control = page.locator(f"[data-filter-id={json.dumps(filter_id)}]")
            if control.count() != 1:
                raise BrowserError(f"no single control for filter {filter_id!r}")
            if control.evaluate("e => e.tagName") == "SELECT":
                control.select_option(value=value)
            else:
                control.fill(value)
                control.dispatch_event("change")
            page.evaluate("() => window.cortino.ready")

        self._run(url, act)

    def read_chart_data(self, url: str, chart_id: str) -> list[dict]:
        rows = self._run(url, lambda page: page.evaluate("id => window.cortino.chartData(id)", chart_id))
        if rows is None:
            raise BrowserError(f"chart {chart_id!r} rendered no data")
        return rows
