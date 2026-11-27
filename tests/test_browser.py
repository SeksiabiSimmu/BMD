"""The extension in a real browser: loaded unpacked into Chromium through Playwright.

Optional suite. It runs only where Playwright (the Python package) and its Chromium build are
installed, and is skipped elsewhere with the reason:

    pip install playwright && playwright install chromium
    python -m unittest test_browser                 # from the tests folder

Environment: ``BMD_TEST_BROWSER=1`` turns "not installed" into a failure (for a machine that is
supposed to have it); ``BMD_TEST_CHROMIUM=<executable>`` loads the extension into another
Chromium-based browser instead of Playwright's build (Brave, for instance); ``BMD_TEST_SHOTS=<folder>``
saves a screenshot of each page there.

What is real here: Manifest V3 registration, the service worker's life, the permissions the browser
actually grants, both pages rendered by the browser's CSS engine, and the content script running in
its isolated world on YouTube-shaped pages. What is not: the pages are served from this file (so
nothing is fetched from YouTube), the native helper is whatever this machine has registered for the
pinned extension ID, and the toolbar popup, context menus and the keyboard shortcut cannot be driven
headlessly. Most checks accept either helper state and say which one they saw; the one that clicks
the page button is skipped when a helper is installed, because the click would add a job to the
real queue.
"""
import json
import os
import shutil
import time
import unittest
from pathlib import Path

from installer_support import EXT_ID
from support import ROOT, tmpdir

DEMANDED = os.environ.get('BMD_TEST_BROWSER') == '1'
SHOTS = os.environ.get('BMD_TEST_SHOTS')
ALARM = 'bmd-v2-queue-watch'
BUTTON = 'brave-music-download-wav'
HOSTS = ('https://www.youtube.com/**', 'https://youtube.com/**', 'https://m.youtube.com/**',
         'https://music.youtube.com/**')
WATCH = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Some track - YouTube</title></head>
<body style="background:#0f0f0f;color:#eee;font:14px Roboto,Arial,sans-serif"><h1 id="title">Some track</h1>
<ytd-watch-metadata><div id="actions" style="display:flex;gap:8px;padding:12px">
<div id="top-level-buttons-computed" style="display:flex;gap:8px"></div></div></ytd-watch-metadata>
</body></html>"""
# Every other page of the mock site: navigation targets, to prove the button follows the URL.
OTHER = ("<!doctype html><html><head><meta charset='utf-8'><title>YouTube</title></head><body><h1>Home</h1>"
         "<ytd-watch-metadata><div id='top-level-buttons-computed'></div></ytd-watch-metadata></body></html>")
PAGES = {'overview': 'Overview', 'downloads': 'Download queue', 'import': 'Playlist import',
         'settings': 'Audio settings', 'diagnostics': 'Diagnostics'}
PROFILES = {'wav': ['24', '24-bit PCM WAV'], 'flac': ['24', '24-bit FLAC'], 'mp3': ['320', 'MP3 · 320 kbps']}
HOST_SETTLED = "() => { const t = document.getElementById('hostStatus').textContent; return !!t && !t.startsWith('Checking'); }"
DASH_SETTLED = "() => { const t = document.getElementById('connection').textContent; return !!t && t !== 'Checking helper'; }"
# Anything laid out beyond the right edge of the viewport and not clipped by a scroller that is
# itself inside it (ids or classes of the first few).
BEYOND_EDGE = """() => {
  const edge = document.documentElement.clientWidth;
  const clipped = (node) => {
    for (let box = node.parentElement; box && box !== document.body; box = box.parentElement)
      if (getComputedStyle(box).overflowX !== 'visible') return box.getBoundingClientRect().right <= edge + 1;
    return false;
  };
  return [...document.querySelectorAll('body *')]
    .filter((node) => node.getClientRects().length && node.getBoundingClientRect().right > edge + 1 && !clipped(node))
    .map((node) => node.id || node.className || node.tagName).slice(0, 8);
}"""


def serve(route):
    """Answer every request for the YouTube hosts from here."""
    url = route.request.url
    body = WATCH if ('/watch?' in url or '/shorts/' in url) else OTHER
    route.fulfill(status=200, content_type='text/html; charset=utf-8', body=body)


def skip(reason):
    raise unittest.SkipTest(reason) if not DEMANDED else AssertionError(reason)


class ExtensionInChromium(unittest.TestCase):
    """One browser for the whole class: launching it is the expensive part."""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            from playwright.sync_api import Error as PlaywrightError
        except ImportError:
            skip('Playwright for Python is not installed (pip install playwright); the browser check did not run')
        cls.extension = ROOT / 'app' / 'extension'
        cls.manifest = json.loads((cls.extension / 'manifest.json').read_text(encoding='utf-8'))
        cls.base = 'chrome-extension://' + EXT_ID + '/'
        cls.folder = tmpdir()
        cls.shots = Path(SHOTS) if SHOTS else None
        if cls.shots:
            cls.shots.mkdir(parents=True, exist_ok=True)
        # Playwright keeps its own scratch files in the temp folder of its driver process. Point that
        # at this class's own folder (removed below) and restore the variable afterwards: the Wine
        # tests of the other modules must not see it.
        cls.saved_tmpdir = os.environ.get('TMPDIR')
        os.environ['TMPDIR'] = str(cls.folder)
        options = {'headless': True}
        if os.environ.get('BMD_TEST_CHROMIUM'):
            options['executable_path'] = os.environ['BMD_TEST_CHROMIUM']
        else:
            options['channel'] = 'chromium'            # the full browser: extensions need it, not the headless shell
        cls.manager = sync_playwright().start()
        try:
            cls.context = cls.manager.chromium.launch_persistent_context(
                str(cls.folder / 'profile'), viewport={'width': 1280, 'height': 800}, **options,
                args=['--disable-extensions-except=' + str(cls.extension), '--load-extension=' + str(cls.extension),
                      '--disable-dev-shm-usage', '--disable-crash-reporter'])
        except PlaywrightError as exc:
            cls.context = None
            cls.tearDownClass()
            skip('Chromium for Playwright could not start (playwright install chromium): ' + str(exc).splitlines()[0])
        cls.browser = cls.context.browser.version if cls.context.browser else 'unknown'
        cls.events = []
        for pattern in HOSTS:
            cls.context.route(pattern, serve)

        worker, deadline = None, time.time() + 30
        while worker is None and time.time() < deadline:
            worker = next((w for w in cls.context.service_workers if w.url.startswith(cls.base)), None)
            if worker is None:
                time.sleep(0.2)
        if worker is None:
            seen = [w.url for w in cls.context.service_workers]
            cls.tearDownClass()
            raise AssertionError('the extension\'s service worker never started; does this browser honour '
                                 '--load-extension? workers seen: ' + str(seen))
        cls.watch(worker, 'service worker')
        cls.worker = worker
        # Which helper this machine has: none at all, a working one, or one that is too old.
        popup = cls.open_popup()
        text = popup.locator('#hostStatus').inner_text()
        popup.close()
        if text.startswith('Connected via Native Messaging'):
            cls.helper = 'installed'
        elif 'setup again to repair it' in text:
            cls.helper = 'absent'
        else:
            cls.helper = 'other: ' + text

    @classmethod
    def restore_tmpdir(cls):
        if cls.saved_tmpdir is None:
            os.environ.pop('TMPDIR', None)
        else:
            os.environ['TMPDIR'] = cls.saved_tmpdir

    @classmethod
    def tearDownClass(cls):
        """Closes the browser and removes the profile; safe to call more than once."""
        manager, cls.manager = getattr(cls, 'manager', None), None
        if manager is not None:
            try:
                if getattr(cls, 'context', None) is not None:
                    cls.context.close()
                    cls.context = None
            finally:
                manager.stop()
        folder, cls.folder = getattr(cls, 'folder', None), None
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
            cls.restore_tmpdir()

    @classmethod
    def watch(cls, target, name):
        """Record every error, warning and uncaught exception a target reports."""
        def note(kind, text):
            cls.events.append(f'{name}: {kind}: {text}')
        target.on('console', lambda message: note('console.' + message.type, message.text)
                  if message.type in ('error', 'warning') else None)
        target.on('pageerror', lambda error: note('exception', error))

    @classmethod
    def page(cls, name):
        """A new, watched tab."""
        page = cls.context.new_page()
        cls.watch(page, name)
        return page

    @classmethod
    def open_popup(cls, width=420):
        """The popup page as a tab (the toolbar popup itself cannot be opened headlessly)."""
        page = cls.page('popup')
        page.set_viewport_size({'width': width, 'height': 640})
        page.goto(cls.base + 'popup.html')
        page.wait_for_function(HOST_SETTLED, timeout=20000, polling=100)
        return page

    @classmethod
    def open_dashboard(cls, width=1280):
        page = cls.page('dashboard')
        page.set_viewport_size({'width': width, 'height': 800})
        page.goto(cls.base + 'dashboard.html')
        page.wait_for_function(DASH_SETTLED, timeout=20000, polling=100)
        return page

    @classmethod
    def shot(cls, page, name):
        if cls.shots:
            page.screenshot(path=str(cls.shots / (name + '.png')), full_page=True)

    # ------------------------------------------------------------------ worker and permissions ----

    def test_the_service_worker_runs_under_the_pinned_id(self):
        self.assertEqual(self.worker.url, self.base + 'background.js')
        self.assertEqual(self.worker.evaluate('() => chrome.runtime.id'), EXT_ID)

    def test_the_browser_meets_the_minimum_version_of_the_manifest(self):
        major = int(self.browser.split('.')[0])
        self.assertGreaterEqual(major, int(self.manifest['minimum_chrome_version']), self.browser)

    def test_the_browser_is_given_the_release_version_and_no_permission_beyond_the_manifest(self):
        manifest = self.worker.evaluate('() => chrome.runtime.getManifest()')
        self.assertEqual(manifest['version'], self.manifest['version'])
        self.assertEqual(manifest['manifest_version'], 3)
        granted = self.worker.evaluate('() => new Promise(done => chrome.permissions.getAll(done))')
        self.assertEqual(sorted(granted['permissions']), sorted(self.manifest['permissions']), granted)
        self.assertEqual(sorted(granted['origins']), sorted(self.manifest['host_permissions']), granted)

    def test_the_queue_alarm_is_registered(self):
        alarms = self.worker.evaluate('() => new Promise(done => chrome.alarms.getAll(done))')
        self.assertEqual([(a['name'], a.get('periodInMinutes')) for a in alarms], [(ALARM, 1)])

    # ------------------------------------------------------------------------------- the pages ----

    def test_the_pages_show_the_installed_version_read_from_the_manifest(self):
        # Regression: the popup of the 2.5.0 build said "v2.4", the 2.3.0 pages said "v2.0".
        expected = 'v' + self.manifest['version']
        popup = self.open_popup()
        self.addCleanup(popup.close)
        self.assertEqual(popup.locator('.version').inner_text(), expected)
        dash = self.open_dashboard()
        self.addCleanup(dash.close)
        self.assertEqual(dash.locator('#appVersion').inner_text(), expected)

    def test_the_popup_fits_its_own_width_in_every_state(self):
        # The popup is 405 px wide by stylesheet (popup.css); the browser never makes it narrower, so
        # the question is whether every pane stays inside that width.
        popup = self.open_popup(405)
        self.addCleanup(popup.close)
        for tab in ('track', 'playlist', 'queue'):
            popup.click('.tab[data-tab="%s"]' % tab)
            layout = popup.evaluate("""() => ({scroll: document.body.scrollWidth, inside: document.body.clientWidth})""")
            self.assertLessEqual(layout['scroll'], layout['inside'], f'{tab} pane: the popup scrolls sideways: {layout}')
            self.assertEqual(popup.evaluate(BEYOND_EDGE), [], f'{tab} pane: elements laid out beyond the right edge')
            self.shot(popup, 'popup-' + tab)
        popup.click('.tab[data-tab="track"]')
        popup.click('#advancedToggle')
        self.assertEqual(popup.evaluate(BEYOND_EDGE), [], 'advanced options open')

    def test_the_popup_reports_the_helper_it_found(self):
        popup = self.open_popup()
        self.addCleanup(popup.close)
        host = popup.locator('#hostStatus').inner_text()
        online = popup.evaluate("() => document.getElementById('connected').classList.contains('online')")
        track = popup.locator('#enqueueTrack')
        if self.helper == 'absent':
            self.assertIn('setup again to repair it', host)
            self.assertFalse(online, 'the dot must not claim a connection')
            self.assertTrue(track.is_disabled(), 'nothing can be queued without the helper')
        elif self.helper == 'installed':
            self.assertNotIn('repair it', host)
            self.assertTrue(online, host)
        else:
            skip('the helper registered here is neither absent nor current: ' + self.helper)

    def test_every_dashboard_page_opens_and_fits_desktop_half_screen_and_narrow_windows(self):
        # Regression (first browser run with a width check): below 763 px the dashboard was wider
        # than its window, because the navigation strip set the minimum width of the whole page.
        dash = self.open_dashboard()
        self.addCleanup(dash.close)
        for width in (1280, 640, 375):
            dash.set_viewport_size({'width': width, 'height': 800})
            for key, title in PAGES.items():
                dash.click('.nav[data-page="%s"]' % key)
                seen = dash.evaluate("""(key) => ({
                    heading: document.getElementById('heading').textContent,
                    shown: [...document.querySelectorAll('.page:not([hidden])')].map((node) => node.id),
                    scroll: document.documentElement.scrollWidth, viewport: document.documentElement.clientWidth})""", key)
                self.assertEqual(seen['heading'], title, f'{width}px, {key}')
                self.assertEqual(seen['shown'], ['page-' + key], f'{width}px, {key}: exactly one page is visible')
                self.assertLessEqual(seen['scroll'], seen['viewport'], f'{width}px, {key}: the page scrolls sideways')
                self.assertEqual(dash.evaluate(BEYOND_EDGE), [], f'{width}px, {key}: elements laid out beyond the right edge')
                if width in (1280, 375):
                    self.shot(dash, f'dashboard-{key}-{width}')
        # A page opened from a button inside the content keeps its navigation entry in view.
        dash.click('.nav[data-page="overview"]')
        dash.click('#goSettings')
        self.assertEqual(dash.locator('#heading').inner_text(), PAGES['settings'])
        self.assertTrue(dash.evaluate("() => document.querySelector('.nav.active').getBoundingClientRect().right <= document.documentElement.clientWidth"),
                        'the active navigation entry is visible in the narrow strip')

    def test_the_overview_describes_the_format_that_was_saved(self):
        # Regression: the Overview described WAV whatever format the user had chosen.
        dash = self.open_dashboard()
        self.addCleanup(dash.close)
        self.addCleanup(dash.evaluate, '() => chrome.storage.local.remove("format")')
        for format, (bits, name) in PROFILES.items():
            dash.evaluate('(format) => chrome.storage.local.set({format})', format)
            dash.reload()
            dash.wait_for_function(DASH_SETTLED, timeout=20000, polling=100)
            shown = [dash.locator('#profileBits').inner_text(), dash.locator('#profileFormat').inner_text()]
            self.assertEqual(shown, [bits, name], f'saved format {format}: the Overview says {shown}')

    def test_changing_the_format_on_the_settings_page_changes_the_overview(self):
        dash = self.open_dashboard()
        self.addCleanup(dash.close)
        self.addCleanup(dash.evaluate, '() => chrome.storage.local.remove("format")')
        dash.click('.nav[data-page="settings"]')
        dash.select_option('#format', 'mp3')
        dash.wait_for_function("() => document.getElementById('settingsStatus').textContent === 'Output format saved.'",
                               timeout=10000, polling=100)
        dash.click('.nav[data-page="overview"]')
        self.assertEqual(dash.locator('#profileFormat').inner_text(), PROFILES['mp3'][1])

    def test_the_overview_says_why_it_is_empty_without_the_helper(self):
        if self.helper != 'absent':
            skip('no helper is missing here (' + self.helper + '); the empty-state text is not shown')
        dash = self.open_dashboard()
        self.addCleanup(dash.close)
        self.assertIn('Nothing to show while the helper is disconnected', dash.locator('#recent').inner_text())

    def test_diagnostics_names_the_connection_it_found(self):
        dash = self.open_dashboard()
        self.addCleanup(dash.close)
        dash.click('.nav[data-page="diagnostics"]')
        dash.click('#checkDependencies')
        dash.wait_for_function("() => ['Connected', 'Disconnected'].includes(document.getElementById('diagnosticConnection').textContent)",
                               timeout=60000, polling=100)
        dash.wait_for_timeout(500)
        line, status = dash.evaluate("""() => [document.getElementById('diagnosticConnection').textContent,
                                                 document.getElementById('diagStatus').textContent]""")
        if self.helper == 'absent':
            self.assertEqual(line, 'Disconnected')
            self.assertRegex(status.lower(), r'setup again')
        elif self.helper == 'installed':
            self.assertEqual(line, 'Connected', status)
            self.assertNotIn('setup again', status.lower())
        else:
            skip('the helper registered here is neither absent nor current: ' + self.helper)

    # ----------------------------------------------------------------------- the page button ----

    def youtube(self):
        page = self.page('youtube')
        page.set_viewport_size({'width': 1280, 'height': 800})
        self.addCleanup(page.close)
        return page

    def test_the_page_button_is_added_to_watch_pages_only(self):
        page = self.youtube()
        page.goto('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        page.wait_for_selector('#' + BUTTON, timeout=15000)
        page.wait_for_timeout(600)
        style = page.evaluate("() => { const s = getComputedStyle(document.getElementById('%s')); return [s.borderRadius, s.padding]; }" % BUTTON)
        self.assertEqual(page.locator('#' + BUTTON).count(), 1, 'exactly one button')
        self.assertEqual(style, ['22px', '9px 13px'], 'the stylesheet of the extension is applied')
        self.shot(page, 'youtube-button')
        page.goto('https://www.youtube.com/')
        page.wait_for_timeout(1500)
        self.assertEqual(page.locator('#' + BUTTON).count(), 0, 'the home page has no button')
        page.evaluate("() => { history.pushState({}, '', '/watch?v=abcdefghij7'); dispatchEvent(new PopStateEvent('popstate')); }")
        page.wait_for_selector('#' + BUTTON, timeout=8000)
        self.assertEqual(page.locator('#' + BUTTON).count(), 1, 'a single-page navigation to a watch page adds it')
        page.evaluate("() => { history.pushState({}, '', '/results?search_query=x'); dispatchEvent(new PopStateEvent('popstate')); }")
        page.wait_for_function("() => !document.getElementById('%s')" % BUTTON, timeout=8000, polling=100)
        self.assertEqual(page.locator('#' + BUTTON).count(), 0, 'a single-page navigation away removes it')
        page.goto('https://www.youtube.com/watch?v=short')
        page.wait_for_timeout(1500)
        self.assertEqual(page.locator('#' + BUTTON).count(), 0, 'a malformed video id gets no button')
        page.goto('https://www.youtube.com/shorts/dQw4w9WgXcQ')
        page.wait_for_selector('#' + BUTTON, timeout=8000)
        self.assertEqual(page.locator('#' + BUTTON).count(), 1, 'a Shorts page gets the button')

    def test_a_click_without_a_helper_is_reported_as_not_queued(self):
        # Regression (first real-browser run): the button said "The helper disconnected before
        # confirming this action. Check the queue before trying again." although the browser had
        # never started the helper and the request had provably not been sent.
        if self.helper != 'absent':
            skip('a helper is installed here (' + self.helper + '); the click would add a job to the real queue')
        page = self.youtube()
        page.goto('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        page.wait_for_selector('#' + BUTTON, timeout=15000)
        page.click('#' + BUTTON)
        page.wait_for_function("() => document.getElementById('%s').textContent === '! Error'" % BUTTON, timeout=20000, polling=100)
        title = page.get_attribute('#' + BUTTON, 'title')
        self.assertIn('setup again to repair it', title)
        self.assertNotIn('before confirming', title, 'a request that was never sent has no unknown outcome')
        page.wait_for_function("() => document.getElementById('%s').textContent === '↓ Save' && !document.getElementById('%s').disabled" % (BUTTON, BUTTON),
                               timeout=10000, polling=100)

    def test_zz_nothing_logged_an_error_a_warning_or_an_exception(self):
        # Runs last (hence the name): it reports what the worker and every page of the suite said.
        self.assertEqual(self.events, [])


if __name__ == '__main__':
    unittest.main()
