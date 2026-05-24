"""
Family focus leaderboard reporter.

Appends completed work blocks to an append-only JSONL log and sends
daily/weekly ranked summaries to a Telegram group chat.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger('family_reporter')


# ------------------------------------------------------------------ #
# Block log — append-only JSONL
# ------------------------------------------------------------------ #

def append_work_block(log_path: str, user_name: str, started_at: datetime,
                      ended_at: datetime, duration_minutes: int,
                      summary: str, tz: ZoneInfo) -> None:
    local_date = ended_at.astimezone(tz).date().isoformat()
    record = {
        'user': user_name.lower(),
        'date': local_date,
        'started_at': started_at.isoformat(),
        'ended_at': ended_at.isoformat(),
        'duration_minutes': duration_minutes,
        'summary': summary,
    }
    with open(log_path, 'a') as f:
        f.write(json.dumps(record) + '\n')
    log.debug(f"Logged block: {user_name} {duration_minutes}min on {local_date}")


def read_blocks_for_dates(log_path: str, date_strs: set, member_names: list) -> dict:
    """Returns {name_lower: {'minutes': int, 'blocks': int}} for all members."""
    stats = {name.lower(): {'minutes': 0, 'blocks': 0} for name in member_names}
    if not os.path.exists(log_path):
        return stats
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            user = rec.get('user', '').lower()
            if rec.get('date') in date_strs and user in stats:
                stats[user]['minutes'] += rec.get('duration_minutes', 0)
                stats[user]['blocks'] += 1
    return stats


# ------------------------------------------------------------------ #
# Leaderboard formatter
# ------------------------------------------------------------------ #

def _fmt_duration(total_minutes: int) -> str:
    if total_minutes < 60:
        return f"{total_minutes} min"
    h, m = divmod(total_minutes, 60)
    return f"{h} hr {m} min" if m else f"{h} hr"


def build_leaderboard_message(period_label: str, stats: dict, member_names: list,
                               header_emoji: str = '📊', show_blocks: bool = True) -> str:
    """stats: {name_lower: {'minutes': int, 'blocks': int}}"""
    order = {name.lower(): i for i, name in enumerate(member_names)}
    ranked = sorted(stats.items(), key=lambda kv: (-kv[1]['minutes'], order.get(kv[0], 999)))
    medals = ['🥇', '🥈', '🥉']
    lines = [f"{header_emoji} Focus Recap — {period_label}", ""]
    medal_rank = 0
    for name, data in ranked:
        minutes = data['minutes']
        blocks = data['blocks']
        if minutes == 0:
            lines.append(f"   {name.title()} — —")
        else:
            prefix = medals[medal_rank] if medal_rank < 3 else f"{medal_rank + 1}."
            block_str = f" ({blocks} block{'s' if blocks != 1 else ''})" if show_blocks else ""
            lines.append(f"{prefix} {name.title()} — {_fmt_duration(minutes)}{block_str}")
            medal_rank += 1
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Sent log — deduplication across restarts
# ------------------------------------------------------------------ #

def _load_sent_log(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_sent_log(path: str, sent: dict) -> None:
    with open(path, 'w') as f:
        json.dump(sent, f, indent=2)


# ------------------------------------------------------------------ #
# Telegram helper
# ------------------------------------------------------------------ #

def _send(token: str, chat_id: int, message: str) -> None:
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={'chat_id': chat_id, 'text': message},
            timeout=10,
        )
        r.raise_for_status()
        log.info(f"Sent to group {chat_id}: {message[:80]!r}...")
    except Exception as e:
        log.warning(f"Group send failed: {e}")


# ------------------------------------------------------------------ #
# Milestone announcements
# ------------------------------------------------------------------ #

_LONG_BLOCK_MIN = 90        # single block threshold
_DAILY_GOALS = [180, 300]   # 3h and 5h day milestones
_STREAK_MIN = 3             # minimum streak to announce


def _compute_streak(log_path: str, user_name: str, today_str: str) -> int:
    # Only counts days where the user hit 5h of focus (300 min)
    daily_totals: dict = {}
    if os.path.exists(log_path):
        with open(log_path) as f:
            for line in f:
                try:
                    rec = json.loads(line.strip())
                    if rec.get('user') == user_name.lower():
                        d = rec['date']
                        daily_totals[d] = daily_totals.get(d, 0) + rec.get('duration_minutes', 0)
                except Exception:
                    pass
    from datetime import date
    streak = 0
    check = date.fromisoformat(today_str)
    while daily_totals.get(check.isoformat(), 0) >= 300:  # 5h threshold
        streak += 1
        check -= timedelta(days=1)
    return streak


def check_and_announce_milestones(log_path: str, sent_log_path: str, user_name: str,
                                   duration_minutes: int, today_str: str,
                                   token: str, group_chat_id: int) -> None:
    sent = _load_sent_log(sent_log_path)
    name = user_name.title()
    messages = []

    # 1. Long single block
    if duration_minutes >= _LONG_BLOCK_MIN:
        messages.append(
            f"🎯 {name} just completed a {_fmt_duration(duration_minutes)} focus block. Locked in!"
        )

    # 2. Daily milestone (3h and 5h) — fire once per user per day per threshold
    daily_total = read_blocks_for_dates(log_path, {today_str}, [user_name]).get(user_name.lower(), {}).get('minutes', 0)
    _goal_emojis = {180: "💪", 300: "🔥"}
    for goal in _DAILY_GOALS:
        key = f"milestone_{goal}min:{user_name.lower()}:{today_str}"
        if daily_total >= goal and key not in sent:
            emoji = _goal_emojis.get(goal, "🔥")
            messages.append(
                f"{emoji} {name} just hit {_fmt_duration(goal)} of focus work today. Way to go!"
            )
            sent[key] = datetime.now(timezone.utc).isoformat()

    # 3. Focus streak (consecutive days with 5h+) — fire once per user per day
    streak_key = f"milestone_streak:{user_name.lower()}:{today_str}"
    if streak_key not in sent:
        streak = _compute_streak(log_path, user_name, today_str)
        if streak >= _STREAK_MIN:
            messages.append(
                f"🔥 {name} is on a {streak}-day focus streak. Keep it up!"
            )
            sent[streak_key] = datetime.now(timezone.utc).isoformat()

    for msg in messages:
        _send(token, group_chat_id, msg)

    if messages:
        _save_sent_log(sent_log_path, sent)


# ------------------------------------------------------------------ #
# Report logic
# ------------------------------------------------------------------ #

def _next_9am(now: datetime, tz: ZoneInfo) -> datetime:
    today_9am = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now < today_9am:
        return today_9am
    return today_9am + timedelta(days=1)


def _maybe_send_daily(now_local: datetime, family_cfg: dict,
                      token: str, sent: dict) -> None:
    yesterday = (now_local.date() - timedelta(days=1)).isoformat()
    key = f"daily:{yesterday}"
    if key in sent:
        return
    member_names = [m['name'] for m in family_cfg['members']]
    totals = read_blocks_for_dates(
        family_cfg['blocks_log_path'], {yesterday}, member_names
    )
    from datetime import date
    d = date.fromisoformat(yesterday)
    label = d.strftime("%A, %B %-d")
    msg = build_leaderboard_message(label, totals, member_names, header_emoji='📊')
    _send(token, family_cfg['group_chat_id'], msg)
    sent[key] = datetime.now(timezone.utc).isoformat()


def _maybe_send_weekly(now_local: datetime, family_cfg: dict,
                       token: str, sent: dict) -> None:
    if now_local.weekday() != 6:  # only on Sunday
        return
    saturday = now_local.date() - timedelta(days=1)
    sunday = saturday - timedelta(days=6)
    date_strs = {(sunday + timedelta(days=i)).isoformat() for i in range(7)}
    iso = saturday.isocalendar()
    key = f"weekly:{iso.year}-W{iso.week:02d}"
    if key in sent:
        return
    member_names = [m['name'] for m in family_cfg['members']]
    totals = read_blocks_for_dates(
        family_cfg['blocks_log_path'], date_strs, member_names
    )
    label = f"Week of {sunday.strftime('%b %-d')}–{saturday.strftime('%b %-d')}"
    msg = build_leaderboard_message(label, totals, member_names, header_emoji='📅')
    _send(token, family_cfg['group_chat_id'], msg)
    sent[key] = datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ #
# Scheduler coroutine
# ------------------------------------------------------------------ #

async def family_reporter_scheduler(config: dict, stop_event: asyncio.Event) -> None:
    family_cfg = config['family']
    token = config['notifications']['telegram']['token']
    tz = ZoneInfo(family_cfg['timezone'])
    sent_log_path = family_cfg.get('sent_log_path', 'family_sent_log.json')

    log.info(f"Family reporter started | tz={family_cfg['timezone']} | group={family_cfg['group_chat_id']}")

    while not stop_event.is_set():
        now_local = datetime.now(tz)
        sent = _load_sent_log(sent_log_path)

        # Check what's due (handles startup catch-up if 9am already passed)
        if now_local.hour >= 9:
            _maybe_send_daily(now_local, family_cfg, token, sent)
            _maybe_send_weekly(now_local, family_cfg, token, sent)
            _save_sent_log(sent_log_path, sent)

        # Sleep until next 9am
        next_fire = _next_9am(datetime.now(tz), tz)
        sleep_secs = (next_fire - datetime.now(tz)).total_seconds()
        log.debug(f"Next report check at {next_fire.strftime('%Y-%m-%d %H:%M %Z')} ({sleep_secs/3600:.1f}h)")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(1.0, sleep_secs))
        except asyncio.TimeoutError:
            pass
