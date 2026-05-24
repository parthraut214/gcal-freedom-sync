#!/usr/bin/env python3
"""
gcal-freedom-sync: polls multiple Google Calendars and starts/stops Freedom.to
sessions via direct HTTP API. Supports multiple users sharing one Freedom account.
"""

import asyncio
import logging
import os
import signal
import sys
import yaml
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from gcal_client import CalendarClient
from freedom_client import FreedomApiClient
from state import StateManager

load_dotenv()


def _setup_logging():
    handlers = [logging.StreamHandler()]
    log_file = os.environ.get('LOG_FILE', 'gcal-freedom-sync.log')
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.DEBUG if '--verbose' in sys.argv else logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=handlers,
    )


def load_config() -> dict:
    path = os.environ.get('CONFIG_PATH', 'config.yaml')
    with open(path) as f:
        return yaml.safe_load(f)


# ------------------------------------------------------------------ #
# Formatting helpers
# ------------------------------------------------------------------ #

def _fmt_duration(total_minutes: int) -> str:
    if total_minutes < 60:
        return f"{total_minutes} min"
    h, m = divmod(total_minutes, 60)
    return f"{h} hr {m} min" if m else f"{h} hr"


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p")


def _next_blocks_msg(cal: CalendarClient, calendars: list) -> str:
    all_events = []
    for cal_cfg in calendars:
        for event in cal.get_upcoming_events(cal_cfg['id'], count=10, hours_ahead=24):
            today = datetime.now(event['start'].tzinfo).date()
            if event['start'].date() == today:
                event['cal_name'] = cal_cfg['name']
                all_events.append(event)
    all_events.sort(key=lambda e: e['start'])
    if not all_events:
        return "No more blocks scheduled for today."
    lines = ["Upcoming blocks today:"]
    for e in all_events:
        duration_min = int((e['end'] - e['start']).total_seconds() / 60)
        lines.append(
            f"• {e['cal_name'].title()}: {e['summary']} — "
            f"{_fmt_time(e['start'])} – {_fmt_time(e['end'])} ({_fmt_duration(duration_min)})"
        )
    return "\n".join(lines)


def _current_block_msg(state: StateManager) -> str:
    if not state.is_active:
        return "No active block right now."
    now = datetime.now(timezone.utc)
    label = f"{state._state.blocklist.title()} Block" if state._state.blocklist else "Block"
    lines = [f"Active: {label} — {state.event_summary}"]
    if state._state.ends_at:
        ends = datetime.fromisoformat(state._state.ends_at)
        remaining_min = max(0, int((ends - now).total_seconds() / 60))
        lines.append(f"Ends at {_fmt_time(ends)} ({_fmt_duration(remaining_min)} remaining)")
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Notifications
# ------------------------------------------------------------------ #

def send_notification(token: str, chat_id: int, message: str):
    import httpx
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={'chat_id': chat_id, 'text': message},
            timeout=10,
        )
        r.raise_for_status()
        logging.getLogger('daemon').info(f"Notification sent to {chat_id}: {message!r}")
    except Exception as e:
        logging.getLogger('daemon').warning(f"Notification failed: {e}")


# ------------------------------------------------------------------ #
# Claude assistant
# ------------------------------------------------------------------ #

def _build_context(state: StateManager, cal: CalendarClient, calendars: list) -> str:
    now = datetime.now(timezone.utc)

    all_events = []
    for cal_cfg in calendars:
        for event in cal.get_upcoming_events(cal_cfg['id'], count=10, hours_ahead=48):
            event['cal_name'] = cal_cfg['name']
            all_events.append(event)
    all_events.sort(key=lambda e: e['start'])

    local_tz = all_events[0]['start'].tzinfo if all_events else timezone.utc
    now_local = now.astimezone(local_tz)
    lines = [f"Current time: {now_local.strftime('%A, %B %-d at %-I:%M %p %Z')}"]

    if state.is_active:
        label = f"{state._state.blocklist.title()} Block" if state._state.blocklist else "Block"
        lines.append(f"Current block: {label} — {state.event_summary}")
        if state._state.ends_at:
            ends = datetime.fromisoformat(state._state.ends_at)
            remaining_min = max(0, int((ends - now).total_seconds() / 60))
            lines.append(f"Ends at: {_fmt_time(ends)} ({_fmt_duration(remaining_min)} remaining)")
    else:
        lines.append("Current block: None (no active block)")

    today_local = now_local.date()
    tomorrow_local = today_local + timedelta(days=1)
    today_events = [e for e in all_events if e['start'].astimezone(local_tz).date() == today_local]
    tomorrow_events = [e for e in all_events if e['start'].astimezone(local_tz).date() == tomorrow_local]

    if today_events:
        lines.append("Upcoming blocks today:")
        for e in today_events:
            duration_min = int((e['end'] - e['start']).total_seconds() / 60)
            lines.append(
                f"  • {e['cal_name'].title()}: {e['summary']} — "
                f"{_fmt_time(e['start'])} – {_fmt_time(e['end'])} ({_fmt_duration(duration_min)})"
            )
    else:
        lines.append("Upcoming blocks today: None")

    if tomorrow_events:
        lines.append("Tomorrow's blocks:")
        for e in tomorrow_events:
            duration_min = int((e['end'] - e['start']).total_seconds() / 60)
            lines.append(
                f"  • {e['cal_name'].title()}: {e['summary']} — "
                f"{_fmt_time(e['start'])} – {_fmt_time(e['end'])} ({_fmt_duration(duration_min)})"
            )

    return "\n".join(lines)


async def ask_claude(user_message: str, state: StateManager,
                     cal: CalendarClient, calendars: list) -> str:
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return "ANTHROPIC_API_KEY not set."
    import anthropic
    context = await asyncio.to_thread(_build_context, state, cal, calendars)
    system = (
        "You are a concise productivity assistant for a focus/sleep block scheduling system. "
        "Answer the user's questions about their schedule precisely. "
        "When listing blocks, always use this format: one short summary sentence, then each block on its own line starting with a bullet (•). "
        "Each bullet line: • <type>: <name> — <start> to <end> (<duration>). "
        "Use plain text only — no markdown, no asterisks.\n\n"
        f"{context}"
    )
    client = anthropic.AsyncAnthropic(api_key=api_key)
    msg = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return msg.content[0].text


# ------------------------------------------------------------------ #
# Telegram bot — single poller, routes by chat_id
# ------------------------------------------------------------------ #

async def telegram_bot_router(config: dict, users_data: list, stop_event: asyncio.Event):
    tg = config.get('notifications', {}).get('telegram')
    if not tg:
        return

    log = logging.getLogger('telegram')
    import httpx

    # Map each user's chat_id to their (user_cfg, cal, state)
    user_by_chat: dict[int, tuple] = {
        user_cfg['telegram_chat_id']: (user_cfg, cal, state)
        for user_cfg, cal, state in users_data
    }

    base_url = f"https://api.telegram.org/bot{tg['token']}"
    token = tg['token']
    offset = 0

    while not stop_event.is_set():
        try:
            async with httpx.AsyncClient(timeout=35) as client:
                r = await client.get(f"{base_url}/getUpdates",
                                     params={'offset': offset, 'timeout': 30})
                updates = r.json().get('result', [])

            for update in updates:
                offset = update['update_id'] + 1
                msg = update.get('message', {})
                text = msg.get('text', '').strip()
                chat_id = msg.get('chat', {}).get('id')
                if not text or chat_id is None:
                    continue

                log.info(f"[chat={chat_id}] Received: {text!r}")

                if chat_id not in user_by_chat:
                    log.warning(f"Unknown chat_id {chat_id} — ignoring")
                    continue

                user_cfg, cal, state = user_by_chat[chat_id]
                calendars = user_cfg['calendars']
                tl = text.lower()

                try:
                    reply = await ask_claude(text, state, cal, calendars)
                except Exception as e:
                    log.warning(f"Claude error: {e}")
                    if any(w in tl for w in ('what block am i in', 'current block', 'am i in a block')):
                        reply = _current_block_msg(state)
                    elif any(w in tl for w in ('upcoming', 'next block', 'next blocks', 'today')):
                        reply = _next_blocks_msg(cal, calendars)
                    else:
                        reply = "I'm temporarily unavailable. Try: 'what block am I in?' or 'upcoming blocks'."

                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(f"{base_url}/sendMessage",
                                      json={'chat_id': chat_id, 'text': reply})
                log.info(f"[chat={chat_id}] Replied: {reply!r}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"Telegram poller error: {e}")
            await asyncio.sleep(5)


# ------------------------------------------------------------------ #
# Per-user daemon loop
# ------------------------------------------------------------------ #

async def run_daemon(config: dict, user_cfg: dict, cal: CalendarClient,
                     state: StateManager, freedom: FreedomApiClient,
                     stop_event: asyncio.Event, family_cfg: dict = None):
    log = logging.getLogger(f"daemon.{user_cfg['name']}")

    calendars = user_cfg['calendars']
    device_ids = user_cfg['device_ids']
    tg_token = config['notifications']['telegram']['token']
    chat_id = user_cfg['telegram_chat_id']
    poll_interval = config['poll']['interval_seconds']
    lookahead = config['poll']['lookahead_seconds']
    thresholds = config['poll'].get('notify_thresholds_seconds', [600, 120])

    notified: dict[int, set] = {t: set() for t in thresholds}
    notified_any: set = set()

    names = [c['name'] for c in calendars]
    log.info(f"Started | user={user_cfg['name']} | calendars={names} | poll={poll_interval}s | thresholds={thresholds}s")

    def notify(message: str):
        send_notification(tg_token, chat_id, message)

    while not stop_event.is_set():
        try:
            now = datetime.now(timezone.utc)

            for cal_cfg in calendars:
                for event in cal.get_upcoming_events(cal_cfg['id'], count=3, hours_ahead=1):
                    seconds_until = (event['start'] - now).total_seconds()
                    eid = event['id']
                    duration_min = int((event['end'] - event['start']).total_seconds() / 60)
                    label = f"{cal_cfg['name'].title()} Block"
                    for threshold in sorted(thresholds, reverse=True):
                        if seconds_until <= threshold and eid not in notified[threshold]:
                            notified[threshold].add(eid)
                            notified_any.add(eid)
                            minutes_until = max(1, round(seconds_until / 60))
                            msg = (f"[UPDATE] {label}: {event['summary']} ({_fmt_duration(duration_min)}) "
                                   f"starting in {minutes_until} min")
                            log.info(f"Pre-notification ({threshold}s): {msg}")
                            notify(msg)
                            notify(_next_blocks_msg(cal, calendars))

            active_event = None
            active_cal = None
            for cal_cfg in calendars:
                event = cal.get_current_event(cal_cfg['id'], lookahead_seconds=lookahead)
                if event:
                    active_event = event
                    active_cal = cal_cfg
                    break

            if active_event and not state.is_active:
                remaining = active_event['end'] - now
                duration_minutes = max(1, int(remaining.total_seconds() / 60))
                locked = active_cal.get('locked_mode', False)
                log.info(
                    f"[{active_cal['name']}] '{active_event['summary']}' → "
                    f"starting {duration_minutes}min block "
                    f"(block_everything={active_cal['block_everything']}, "
                    f"block_apps={active_cal['block_apps']}, locked={locked})"
                )
                schedule_id = freedom.start_session(
                    filter_list_ids=active_cal['filter_list_ids'],
                    device_ids=device_ids,
                    duration_minutes=duration_minutes,
                    block_apps=active_cal['block_apps'],
                    block_everything=active_cal['block_everything'],
                    locked_mode=locked,
                )
                if schedule_id is not None:
                    state.set_active(
                        event_id=active_event['id'],
                        event_summary=active_event['summary'],
                        blocklist=active_cal['name'],
                        ends_at=active_event['end'].isoformat(),
                        schedule_id=schedule_id,
                        locked=locked,
                    )
                    if active_event['id'] not in notified_any:
                        notified_any.add(active_event['id'])
                        total_min = int((active_event['end'] - active_event['start']).total_seconds() / 60)
                        label = f"{active_cal['name'].title()} Block"
                        msg = f"[UPDATE] {label}: {active_event['summary']} ({_fmt_duration(total_min)}) starting now"
                        notify(msg)
                        notify(_next_blocks_msg(cal, calendars))
                else:
                    log.error("start_session returned no schedule_id — will retry next poll")

            elif not active_event and state.is_active:
                started = datetime.fromisoformat(state.started_at) if state.started_at else None
                actual_min = int((now - started).total_seconds() / 60) if started else 0
                blocklist_name = state._state.blocklist  # capture before set_inactive() clears state
                event_summary_captured = state.event_summary
                label = f"{blocklist_name.title()} Block" if blocklist_name else "Block"
                end_msg = f"[UPDATE] {label} complete: {event_summary_captured} ({_fmt_duration(actual_min)})"

                def _log_block_if_work():
                    if blocklist_name == "work" and family_cfg and started:
                        from zoneinfo import ZoneInfo
                        from family_reporter import append_work_block, check_and_announce_milestones
                        tz = ZoneInfo(family_cfg['timezone'])
                        today_str = now.astimezone(tz).date().isoformat()
                        append_work_block(
                            log_path=family_cfg['blocks_log_path'],
                            user_name=user_cfg['name'],
                            started_at=started,
                            ended_at=now,
                            duration_minutes=actual_min,
                            summary=event_summary_captured or "",
                            tz=tz,
                        )
                        check_and_announce_milestones(
                            log_path=family_cfg['blocks_log_path'],
                            sent_log_path=family_cfg.get('sent_log_path', 'family_sent_log.json'),
                            user_name=user_cfg['name'],
                            duration_minutes=actual_min,
                            today_str=today_str,
                            token=config['notifications']['telegram']['token'],
                            group_chat_id=family_cfg['group_chat_id'],
                        )

                if state.locked:
                    log.info(f"Event ended → locked session, Freedom timer will handle stop (schedule={state.schedule_id})")
                    state.set_inactive()
                    _log_block_if_work()
                    notify(end_msg)
                else:
                    log.info(f"Event ended → stopping Freedom block (schedule={state.schedule_id})")
                    ok = freedom.stop_session(state.schedule_id) if state.schedule_id else True
                    if ok:
                        state.set_inactive()
                        _log_block_if_work()
                        notify(end_msg)
                    else:
                        log.error("stop_session failed — will retry next poll")

            elif active_event:
                log.debug(
                    f"[{active_cal['name']}] holding block for '{active_event['summary']}' "
                    f"until {active_event['end'].strftime('%H:%M %Z')}"
                )
            else:
                log.debug("Idle")

        except Exception as e:
            log.error(f"Poll error: {e}", exc_info=True)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass

    log.info(f"Daemon stopped for user={user_cfg['name']}")


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #

async def main():
    _setup_logging()
    config = load_config()

    freedom = FreedomApiClient(session_path=config['freedom']['session_path'])

    users_data = []
    for user_cfg in config['users']:
        cal = CalendarClient(
            credentials_path=user_cfg['google_credentials_path'],
            token_path=user_cfg['google_token_path'],
        )
        state = StateManager(user_cfg['state_path'])
        users_data.append((user_cfg, cal, state))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    from family_reporter import family_reporter_scheduler

    family_cfg = config.get('family')
    tasks = [
        run_daemon(config, user_cfg, cal, state, freedom, stop_event, family_cfg)
        for user_cfg, cal, state in users_data
    ]
    tasks.append(telegram_bot_router(config, users_data, stop_event))
    if family_cfg:
        tasks.append(family_reporter_scheduler(config, stop_event))

    await asyncio.gather(*tasks)


if __name__ == '__main__':
    asyncio.run(main())
