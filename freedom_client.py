"""
Freedom.to API client — direct HTTP calls, no browser automation for normal operation.

Auth: session cookies from freedom_session.json (generated once via --setup-session).
CSRF:  fetched from the dashboard HTML before each mutating request.

Playwright is only used for the one-time --setup-session and --sniff tools.
"""

import json
import logging
import os
import re
import sys
import asyncio
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

BASE = 'https://freedom.to'
DASHBOARD_URL = f'{BASE}/dashboard'
LOGIN_URL = f'{BASE}/log-in'


# ------------------------------------------------------------------ #
# HTTP API client (used by the daemon)
# ------------------------------------------------------------------ #

class FreedomApiClient:
    def __init__(self, session_path: str = 'freedom_session.json'):
        self.session_path = session_path

    def _cookies(self) -> dict:
        state = json.loads(Path(self.session_path).read_text())
        return {c['name']: c['value']
                for c in state.get('cookies', [])
                if 'freedom.to' in c.get('domain', '')}

    def _make_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=BASE,
            cookies=self._cookies(),
            headers={
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': DASHBOARD_URL,
                'Accept': 'application/json, text/javascript, */*',
            },
            follow_redirects=True,
            timeout=30,
        )

    def _get_csrf_token(self, client: httpx.Client) -> str:
        r = client.get('/dashboard')
        if 'log-in' in str(r.url):
            raise RuntimeError(
                "Freedom session expired. Re-run:\n"
                "  python freedom_client.py --setup-session\n"
                "then copy freedom_session.json to the Pi."
            )
        m = re.search(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"', r.text)
        if not m:
            raise RuntimeError("Could not find CSRF token in Freedom dashboard HTML")
        return m.group(1)

    def start_session(self, filter_list_ids: list, device_ids: list,
                      duration_minutes: int, block_apps: bool = False,
                      block_everything: bool = False,
                      locked_mode: bool = False) -> Optional[str]:
        with self._make_client() as client:
            csrf = self._get_csrf_token(client)
            if locked_mode:
                client.patch('/settings/', json={'strong_mode': True},
                             headers={'X-CSRF-Token': csrf}).raise_for_status()
            payload = {
                'filter_list_ids': filter_list_ids,
                'device_ids': device_ids,
                'block_everything': block_everything,
                'block_apps': block_apps,
                'duration': duration_minutes * 60,
                'start_time': 'now',
            }
            r = client.post('/schedules/', json=payload,
                            headers={'X-CSRF-Token': csrf})
            logger.debug(f"POST /schedules/ → {r.status_code}: {r.text[:400]}")
            r.raise_for_status()

            data = r.json()
            schedule_id = (data.get('id') or
                           (data.get('schedule') or {}).get('id') or
                           data.get('schedule_id'))
            if schedule_id:
                logger.info(f"Session started (schedule_id={schedule_id})")
            else:
                logger.warning(f"Session started but no schedule_id in response: {data}")
            return str(schedule_id) if schedule_id else None

    def stop_session(self, schedule_id: str) -> bool:
        with self._make_client() as client:
            csrf = self._get_csrf_token(client)
            r = client.post(f'/schedules/{schedule_id}/end', json={},
                            headers={'X-CSRF-Token': csrf})
            logger.debug(f"POST /schedules/{schedule_id}/end → {r.status_code}")
            if r.status_code == 404:
                logger.info(f"Session {schedule_id} already ended (Freedom timer)")
                return True
            ok = r.status_code < 400
            if ok:
                logger.info(f"Session {schedule_id} stopped")
            return ok


# ------------------------------------------------------------------ #
# One-time session setup (needs real Chrome to bypass Google auth)
# ------------------------------------------------------------------ #

async def _setup_session():
    from playwright.async_api import async_playwright
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    session_path = 'freedom_session.json'

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel='chrome', headless=False,
            args=['--disable-blink-features=AutomationControlled'],
        )
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(LOGIN_URL, wait_until='domcontentloaded')

        print("\nBrowser opened. Click 'Continue with Google' and sign in.")
        print("Once you're on the Freedom.to dashboard, come back here and press Enter.")
        input("Press Enter when logged in...")

        await context.storage_state(path=session_path)
        await browser.close()

    print(f"\nSession saved to {session_path!r}")
    print(f"Copy to the Pi:")
    print(f"  scp {session_path} root@192.168.1.147:/opt/gcal-freedom-sync/")


# ------------------------------------------------------------------ #
# Sniff mode — captures raw API calls for inspection
# ------------------------------------------------------------------ #

async def _sniff_run():
    from playwright.async_api import async_playwright
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    session_path = 'freedom_session.json'
    captured = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel='chrome', headless=False,
            args=['--disable-blink-features=AutomationControlled'],
        )
        storage = session_path if os.path.exists(session_path) else None
        context = await browser.new_context(storage_state=storage)
        page = await context.new_page()

        async def on_response(response):
            if 'freedom.to' in response.url and not any(
                    x in response.url for x in ('google', 'nr-data', 'analytics')):
                req = response.request
                if req.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                    try:
                        body = await response.text()
                    except Exception:
                        body = ''
                    captured.append({
                        'method': req.method,
                        'url': response.url,
                        'status': response.status,
                        'request_body': req.post_data,
                        'response_body': body[:1000],
                    })
                    print(f"\n>>> {req.method} {response.url} → {response.status}")
                    if req.post_data:
                        print(f"    req:  {req.post_data[:300]}")
                    if body:
                        print(f"    resp: {body[:300]}")

        page.on('response', on_response)
        await page.goto(DASHBOARD_URL, wait_until='domcontentloaded')

        print("\nDashboard open. Manually START and STOP a session in the browser.")
        print("API calls (with responses) will be printed here. Press Enter when done.")
        input()

        with open('freedom_api_calls.json', 'w') as f:
            json.dump(captured, f, indent=2)
        print(f"\nSaved {len(captured)} calls to freedom_api_calls.json")
        await browser.close()


# ------------------------------------------------------------------ #
# CLI entry point
# ------------------------------------------------------------------ #

if __name__ == '__main__':
    # python freedom_client.py --setup-session   → save Google login cookies
    # python freedom_client.py --sniff           → capture API calls + responses
    if '--setup-session' in sys.argv:
        asyncio.run(_setup_session())
    elif '--sniff' in sys.argv:
        asyncio.run(_sniff_run())
    else:
        print("Usage: python freedom_client.py --setup-session | --sniff")
