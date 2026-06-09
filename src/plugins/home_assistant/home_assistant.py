import base64
import io
import json
import logging
from urllib.parse import urljoin, urlparse

from PIL import Image

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

# Lean Chromium launch flags, mirroring utils.image_utils.take_screenshot, to keep
# memory usage low enough to run on a Raspberry Pi Zero 2 W (512MB RAM).
CHROMIUM_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--use-gl=swiftshader",
    "--hide-scrollbars",
    "--in-process-gpu",
    "--js-flags=--jitless",
    "--disable-zero-copy",
    "--disable-gpu-memory-buffer-compositor-resources",
    "--disable-extensions",
    "--disable-plugins",
    "--mute-audio",
    "--renderer-process-limit=1",
    "--no-zygote",
    "--no-sandbox",
]

# Far-future expiry (ms) so the Home Assistant frontend never tries to refresh the
# long-lived access token (which has no refresh token).
TOKEN_EXPIRES_MS = 9999999999999


class HomeAssistant(BasePlugin):
    """Render a Home Assistant Lovelace dashboard for the display.

    Authenticates by injecting a long-lived access token into the browser's
    localStorage (the same mechanism the HA frontend uses), then screenshots the
    dashboard. This replaces running the Lovelace Kindle Screensaver add-on.
    """

    def generate_image(self, settings, device_config):
        base_url = (settings.get("ha_url") or "").strip().rstrip("/")
        dashboard_path = (settings.get("dashboard_path") or "").strip()
        token = (settings.get("access_token") or "").strip()

        if not base_url:
            raise RuntimeError("Home Assistant URL is required.")
        if not token:
            raise RuntimeError("A long-lived access token is required.")

        try:
            delay_ms = int(settings.get("render_delay") or 2000)
        except (TypeError, ValueError):
            delay_ms = 2000

        try:
            ready_timeout_ms = int(settings.get("ready_timeout") or 180000)
        except (TypeError, ValueError):
            ready_timeout_ms = 180000

        target_url = urljoin(base_url + "/", dashboard_path.lstrip("/")) if dashboard_path else base_url

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        # HA's dashboard is responsive: rendering at the panel's exact (small) size reflows
        # to a sparser, zoomed-in layout that clips content. Rendering at a larger size and
        # downscaling makes more fit (like the Kindle Screensaver's RENDERING_SCREEN_WIDTH/
        # HEIGHT). Note: the render size is also the raster size, so very large values can
        # OOM a Pi Zero 2 W. Defaults to the panel size (no scaling).
        render_dims = self._render_dimensions(settings, dimensions)

        logger.info(
            f"Capturing Home Assistant dashboard: {target_url} "
            f"(render {render_dims[0]}x{render_dims[1]} -> display {dimensions[0]}x{dimensions[1]})"
        )

        image = self._capture(base_url, target_url, token, render_dims, delay_ms, ready_timeout_ms)
        if not image:
            raise RuntimeError("Failed to capture Home Assistant dashboard, please check logs.")

        if image.size != tuple(dimensions):
            image = image.resize((dimensions[0], dimensions[1]), Image.LANCZOS)
        return image

    @staticmethod
    def _render_dimensions(settings, dimensions):
        """Resolve the browser render size from settings, falling back to the panel size."""
        def _positive_int(value):
            try:
                value = int(value)
                return value if value > 0 else None
            except (TypeError, ValueError):
                return None

        width = _positive_int(settings.get("render_width"))
        height = _positive_int(settings.get("render_height"))
        return [width or dimensions[0], height or dimensions[1]]

    def _capture(self, base_url, target_url, token, dimensions, delay_ms, ready_timeout_ms):
        try:
            from playwright.sync_api import sync_playwright
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        except ImportError:
            raise RuntimeError(
                "Playwright is not installed. Run 'pip install playwright' and "
                "'playwright install chromium' to use the Home Assistant plugin."
            )

        # Build the hassTokens object the HA frontend expects in localStorage.
        origin = "{0.scheme}://{0.netloc}".format(urlparse(base_url))
        hass_tokens = json.dumps({
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": 1800,
            "hassUrl": origin,
            "clientId": origin + "/",
            "expires": TOKEN_EXPIRES_MS,
            "refresh_token": "",
        })
        init_script = (
            "try {{ window.localStorage.setItem('hassTokens', {0}); }} catch (e) {{}}"
            .format(json.dumps(hass_tokens))
        )

        # Ready when the frontend is past its "Loading data" splash (home-assistant-main has
        # mounted), no loading spinners remain, AND at least one Lovelace card has actually
        # PAINTED (a *-card element with real height). Waiting only for "no spinner" fires too
        # early on slow hardware -- the shell is up but cards haven't rendered yet -- producing
        # a blank capture. Recurses through open shadow roots (the HA UI is all web components).
        ready_js = """
        () => {
          let spinner = false, painted = false;
          const walk = (root) => {
            for (const el of root.querySelectorAll('*')) {
              const tag = el.localName || '';
              if (tag.includes('circular-progress') || tag.includes('spinner')) spinner = true;
              if (tag.endsWith('-card') && el.getBoundingClientRect().height > 80) painted = true;
              if (el.shadowRoot) walk(el.shadowRoot);
            }
          };
          const ha = document.querySelector('home-assistant');
          if (!ha || !ha.shadowRoot) return false;
          if (!ha.shadowRoot.querySelector('home-assistant-main')) return false;
          walk(document);
          return painted && !spinner;
        }
        """

        image = None
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            try:
                context = browser.new_context(
                    viewport={"width": dimensions[0], "height": dimensions[1]},
                    ignore_https_errors=True,
                )
                # Runs before any page script on every navigation, so HA boots authenticated.
                context.add_init_script(init_script)
                page = context.new_page()
                page.goto(target_url, wait_until="domcontentloaded", timeout=90000)
                try:
                    # Generous timeout: rendering HA's JS-heavy frontend in software-GL
                    # Chromium on low-power hardware (Pi Zero 2 W) can take well over a
                    # minute. Poll on a fixed interval rather than every animation frame so
                    # the readiness check itself doesn't steal CPU from the slow render.
                    page.wait_for_function(ready_js, timeout=ready_timeout_ms, polling=2000)
                except PlaywrightTimeoutError:
                    logger.warning("Home Assistant did not finish loading in time; capturing anyway.")
                # Let the Lovelace view and its cards finish painting.
                if delay_ms > 0:
                    page.wait_for_timeout(delay_ms)
                # Capture via CDP rather than page.screenshot(): the latter blocks on
                # document.fonts.ready, which can hang indefinitely on the HA frontend
                # (slow/never-resolving icon webfonts) on low-power hardware.
                cdp = context.new_cdp_session(page)
                result = cdp.send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
                png_bytes = base64.b64decode(result["data"])
                image = Image.open(io.BytesIO(png_bytes)).copy()
            finally:
                browser.close()
        return image
