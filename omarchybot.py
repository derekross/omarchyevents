#!/usr/bin/env python3
"""Omarchy Events: mirror the Omarchy Meetups calendar on Luma to Nostr (NIP-52).

Unofficial community mirror. Not affiliated with Omarchy or the Omacom Foundation.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import time
import unicodedata
from typing import Optional
from zoneinfo import ZoneInfo

import requests

RELAYS = [
    "wss://relay.ditto.pub",
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.primal.net",
]

# The Omarchy Meetups calendar: https://luma.com/omarchy (what omarchy.org/meetups renders).
CALENDAR_API_ID = "cal-SDGGMsEps9ExsrT"
CALENDAR_SLUG = "omarchy"
ITEMS_URL = "https://api.lu.ma/calendar/get-items"
EVENT_URL = "https://api.lu.ma/event/get"
LUMA_EVENT_BASE = "https://luma.com/"
CALENDAR_URL = "https://luma.com/omarchy"
MEETUPS_PAGE = "https://omarchy.org/meetups/"
USER_AGENT = "omarchy-events-bot/1.0 (+https://github.com/derekross/omarchyevents)"

CALENDAR_D_TAG = "omarchy-meetups"
MAX_SWEEP = 10           # refuse to delete more than this per run unless --force-sweep
ANNOUNCE_LIMIT = 3       # kind 1 announcements per run, so a first seed doesn't flood feeds

CONFIG_DIR = pathlib.Path(os.path.expanduser("~/.config/omarchyevents"))
NSEC_PATH = CONFIG_DIR / "nsec"
STATE_PATH = CONFIG_DIR / "state.json"

PROFILE = {
    "name": "Omarchy Events",
    "display_name": "Omarchy Events",
    "about": (
        "Every Omarchy meetup on the global calendar (luma.com/omarchy), mirrored to Nostr "
        "as calendar events you can follow from any NIP-52 client. "
        "Unofficial community mirror, not affiliated with Omarchy or the Omacom Foundation. "
        "Meetups are community-run; RSVP on Luma."
    ),
    "website": MEETUPS_PAGE,
    # assets/avatar.svg, uploaded to Blossom with the bot's key. Not the Omarchy logo (trademark).
    "picture": os.environ.get(
        "OMARCHYEVENTS_PICTURE",
        "https://nostr.download/ce43068152aa93bc9713417aec695c461f8e2a9cee90a6bff2f9152e9c02452b.png",
    ),
}

log = logging.getLogger("omarchybot")


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

def http_get_json(url: str, params: Optional[dict] = None) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last_exc = e
            backoff = 2 ** attempt
            log.warning("HTTP %s failed (%s), retry in %ds", url, e, backoff)
            time.sleep(backoff)
    raise RuntimeError(f"http_get_json failed for {url}: {last_exc}")


def fetch_calendar(period: str = "future") -> list[dict]:
    """All entries of the calendar. Fails loudly if Luma's (unofficial) shape changes."""
    entries: list[dict] = []
    cursor: Optional[str] = None
    for _ in range(20):
        params = {"calendar_api_id": CALENDAR_API_ID, "period": period, "pagination_limit": 100}
        if cursor:
            params["pagination_cursor"] = cursor
        data = http_get_json(ITEMS_URL, params)
        if not isinstance(data, dict) or "entries" not in data:
            raise RuntimeError(f"unexpected calendar payload keys: {list(data)[:10]}")
        page = data["entries"]
        for e in page:
            if "event" not in e or "api_id" not in e["event"]:
                raise RuntimeError("unexpected entry shape: no event.api_id")
        entries.extend(page)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return entries


def fetch_event_detail(api_id: str) -> dict:
    return http_get_json(EVENT_URL, {"event_api_id": api_id})


# ----------------------------------------------------------------------------
# ProseMirror (Luma's description_mirror) -> plain text with a little markdown
# ----------------------------------------------------------------------------

def _pm_type(node: dict) -> str:
    """Luma emits both camelCase and snake_case node types; normalise to camelCase."""
    t = node.get("type") or ""
    return re.sub(r"_([a-z])", lambda m: m.group(1).upper(), t)


def _pm_inline(node: dict) -> str:
    t = _pm_type(node)
    if t == "text":
        text = node.get("text", "")
        for mark in node.get("marks", []) or []:
            if mark.get("type") == "link":
                href = (mark.get("attrs") or {}).get("href", "")
                if href and href != text and not text.startswith("http"):
                    text = f"{text} ({href})"
        return text
    if t == "hardBreak":
        return "\n"
    if t == "mention":
        return (node.get("attrs") or {}).get("label", "") or (node.get("attrs") or {}).get("name", "")
    return "".join(_pm_inline(c) for c in node.get("content", []) or [])


def _pm_block(node: dict, depth: int = 0) -> str:
    t = _pm_type(node)
    kids = node.get("content", []) or []
    if t in ("paragraph",):
        return _pm_inline(node).strip() + "\n\n"
    if t == "heading":
        return _pm_inline(node).strip() + "\n\n"
    if t in ("bulletList", "orderedList"):
        out = []
        for i, item in enumerate(kids, 1):
            marker = f"{i}." if t == "orderedList" else "-"
            body = "".join(_pm_block(c, depth + 1) for c in item.get("content", []) or []).strip()
            out.append(f"{'  ' * depth}{marker} {body}")
        return "\n".join(out) + "\n\n"
    if t == "blockquote":
        inner = "".join(_pm_block(c, depth) for c in kids).strip()
        return "\n".join("> " + line for line in inner.splitlines()) + "\n\n"
    if t in ("codeBlock",):
        return _pm_inline(node) + "\n\n"
    if t == "horizontalRule":
        return "---\n\n"
    if t in ("image",):
        src = (node.get("attrs") or {}).get("src", "")
        return (src + "\n\n") if src else ""
    # doc, unknown containers
    return "".join(_pm_block(c, depth) for c in kids)


def prosemirror_to_text(doc: Optional[dict]) -> str:
    if not doc:
        return ""
    s = _pm_block(doc)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

GEOHASH_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash(lat: float, lng: float, precision: int = 9) -> str:
    lat_lo, lat_hi = -90.0, 90.0
    lng_lo, lng_hi = -180.0, 180.0
    bits: list[int] = []
    target = precision * 5
    while len(bits) < target:
        if len(bits) % 2 == 0:
            mid = (lng_lo + lng_hi) / 2
            if lng > mid:
                bits.append(1)
                lng_lo = mid
            else:
                bits.append(0)
                lng_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat > mid:
                bits.append(1)
                lat_lo = mid
            else:
                bits.append(0)
                lat_hi = mid
    out = []
    for i in range(0, len(bits), 5):
        idx = 0
        for b in bits[i:i + 5]:
            idx = (idx << 1) | b
        out.append(GEOHASH_BASE32[idx])
    return "".join(out)


_SLUG_FIX = str.maketrans({"ı": "i", "ø": "o", "ß": "ss", "æ": "ae", "œ": "oe", "đ": "d", "ł": "l", "þ": "th"})


def slugify_tag(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower().translate(_SLUG_FIX))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def first_sentence(text: str, limit: int = 200) -> str:
    para = next((p.strip() for p in text.split("\n\n") if p.strip()), "")
    para = re.sub(r"\s+", " ", para)
    if len(para) <= limit:
        return para
    cut = para[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(".,;:") + "…"


ONLINE_TYPES = {"meet", "zoom", "online", "virtual", "google_meet", "teams", "youtube", "twitch", "custom"}


def resolve_location(ev: dict) -> tuple[Optional[str], Optional[tuple[float, float]], int, Optional[dict]]:
    """Returns (location string, (lat,lng) or None, geohash precision, geo dict)."""
    ltype = ev.get("location_type") or "unknown"
    geo = ev.get("geo_address_info") or None
    vis = ev.get("geo_address_visibility") or "public"
    coord = ev.get("coordinate") or None
    latlng = None
    if coord and coord.get("latitude") is not None and coord.get("longitude") is not None:
        latlng = (float(coord["latitude"]), float(coord["longitude"]))

    if ltype in ONLINE_TYPES:
        return "Online (link on Luma after RSVP)", None, 0, geo

    if not geo:
        return None, None, 0, None

    city_level = geo.get("city_state") or ", ".join(
        p for p in (geo.get("city"), geo.get("country")) if p
    ) or None

    if vis == "public":
        loc = geo.get("full_address") or geo.get("address") or city_level
        return loc, latlng, 9, geo
    # guests-only: publish the city, and only a coarse (~5 km) geohash
    return city_level, latlng, 5, geo


# ----------------------------------------------------------------------------
# Events
# ----------------------------------------------------------------------------

@dataclasses.dataclass
class NostrEvent:
    kind: int
    d_tag: Optional[str]
    title: str
    tags: list[tuple[str, str]]
    content: str

    def canonical_hash(self) -> str:
        payload = json.dumps(
            {"kind": self.kind, "d": self.d_tag, "tags": sorted(self.tags), "content": self.content},
            sort_keys=True, ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclasses.dataclass
class Meetup:
    api_id: str
    name: str
    start: dt.datetime
    end: Optional[dt.datetime]
    tzid: str
    url: str
    cover: Optional[str]
    hosts: list[str]
    location: Optional[str]
    latlng: Optional[tuple[float, float]]
    geo_precision: int
    city: Optional[str]
    country_code: Optional[str]
    description: str

    @property
    def d_tag(self) -> str:
        return f"luma-{self.api_id}"

    def local_start(self) -> dt.datetime:
        try:
            return self.start.astimezone(ZoneInfo(self.tzid))
        except Exception:
            return self.start


def build_meetup(entry: dict, detail: Optional[dict]) -> Meetup:
    ev = entry["event"]
    slug = ev.get("url") or ev["api_id"]
    url = slug if slug.startswith("http") else LUMA_EVENT_BASE + slug
    hosts = [h.get("name") for h in (detail or entry).get("hosts", []) or [] if h.get("name")]
    location, latlng, prec, geo = resolve_location(ev)
    desc = prosemirror_to_text((detail or {}).get("description_mirror"))
    end = parse_iso(ev["end_at"]) if ev.get("end_at") else None
    return Meetup(
        api_id=ev["api_id"],
        name=(ev.get("name") or "Omarchy Meetup").strip(),
        start=parse_iso(ev["start_at"]),
        end=end,
        tzid=ev.get("timezone") or "UTC",
        url=url,
        cover=ev.get("cover_url") or (entry.get("cover_image") or {}).get("url"),
        hosts=hosts,
        location=location,
        latlng=latlng,
        geo_precision=prec,
        city=(geo or {}).get("city"),
        country_code=(geo or {}).get("country_code"),
        description=desc,
    )


def build_calendar_event(m: Meetup) -> NostrEvent:
    tags: list[tuple[str, str]] = [
        ("title", m.name),
        ("start", str(int(m.start.timestamp()))),
    ]
    if m.end and m.end > m.start:
        tags.append(("end", str(int(m.end.timestamp()))))
    tags.append(("start_tzid", m.tzid))
    if m.end:
        tags.append(("end_tzid", m.tzid))

    summary = first_sentence(m.description) if m.description else ""
    if not summary:
        where = f" in {m.city}" if m.city else ""
        summary = f"{m.name}: an Omarchy community meetup{where}."
    tags.append(("summary", summary))
    if m.cover:
        tags.append(("image", m.cover))
    if m.location:
        tags.append(("location", m.location))
    if m.latlng and m.geo_precision:
        tags.append(("g", geohash(m.latlng[0], m.latlng[1], m.geo_precision)))

    hashtags = ["omarchy", "omarchymeetup", "linux", "meetup"]
    if m.city:
        hashtags.append(slugify_tag(m.city))
    if m.country_code:
        hashtags.append(m.country_code.lower())
    seen: set[str] = set()
    for h in hashtags:
        if h and h not in seen:
            seen.add(h)
            tags.append(("t", h))
    tags.append(("r", m.url))

    parts = [m.description] if m.description else []
    if m.hosts:
        parts.append("Hosted by " + ", ".join(m.hosts) + ".")
    parts.append(f"RSVP and details: {m.url}")
    parts.append(f"All Omarchy meetups: {CALENDAR_URL}")
    content = "\n\n".join(parts)

    return NostrEvent(kind=31923, d_tag=m.d_tag, title=m.name, tags=tags, content=content)


def build_calendar(pubkey: str, meetups: list[Meetup]) -> NostrEvent:
    tags: list[tuple[str, str]] = [("title", "Omarchy Meetups")]
    for m in sorted(meetups, key=lambda x: x.start):
        tags.append(("a", f"31923:{pubkey}:{m.d_tag}"))
    tags.extend([("t", "omarchy"), ("t", "omarchymeetup"), ("r", CALENDAR_URL), ("r", MEETUPS_PAGE)])
    content = (
        "Community-run Omarchy meetups around the world, mirrored from the global calendar at "
        f"{CALENDAR_URL}. Unofficial mirror; not affiliated with Omarchy or the Omacom Foundation."
    )
    return NostrEvent(kind=31924, d_tag=CALENDAR_D_TAG, title="Omarchy Meetups", tags=tags, content=content)


def build_announcement(m: Meetup, naddr: str) -> NostrEvent:
    when = m.local_start().strftime("%a %b %-d, %-I:%M %p %Z").replace(" 0", " ")
    where = m.city or (m.location if m.location and m.location.startswith("Online") else None)
    line2 = " · ".join(p for p in (when, where) if p)
    content = (
        f"New Omarchy meetup: {m.name}\n{line2}\n\n"
        f"nostr:{naddr}\n\nRSVP: {m.url}\n\n#omarchy #omarchymeetup"
    )
    tags: list[tuple[str, str]] = [("t", "omarchy"), ("t", "omarchymeetup"), ("r", m.url),
                                   ("a", naddr_to_a(naddr) or "")]
    tags = [t for t in tags if t[1]]
    return NostrEvent(kind=1, d_tag=None, title=m.name, tags=tags, content=content)


# ----------------------------------------------------------------------------
# nak
# ----------------------------------------------------------------------------

def load_nsec() -> str:
    if not NSEC_PATH.exists():
        raise SystemExit(f"nsec not found at {NSEC_PATH}. Run setup.sh first.")
    return NSEC_PATH.read_text().strip()


def pubkey_from_nsec(nsec: str) -> str:
    r = subprocess.run(["nak", "key", "public", nsec], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"nak key public failed: {r.stderr.strip()}")
    return r.stdout.strip()


_NADDR_CACHE: dict[str, str] = {}


def encode_naddr(pubkey: str, kind: int, d: str) -> str:
    key = f"{kind}:{pubkey}:{d}"
    if key in _NADDR_CACHE:
        return _NADDR_CACHE[key]
    r = subprocess.run(
        ["nak", "encode", "naddr", "-d", d, "-a", pubkey, "-k", str(kind), "-r", RELAYS[0]],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError(f"nak encode naddr failed: {r.stderr.strip()}")
    _NADDR_CACHE[key] = r.stdout.strip()
    _NADDR_CACHE[_NADDR_CACHE[key]] = key
    return _NADDR_CACHE[key]


def naddr_to_a(naddr: str) -> Optional[str]:
    return _NADDR_CACHE.get(naddr)


def publish_event(ev: NostrEvent, *, nsec: str) -> bool:
    content = ev.content
    if content.startswith("@"):
        content = " " + content
    cmd = ["nak", "event", "-k", str(ev.kind), "-c", content]
    if ev.d_tag:
        cmd.extend(["-d", ev.d_tag])
    for k, v in ev.tags:
        cmd.extend(["-t", f"{k}={v}"])
    cmd.extend(RELAYS)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            env={**os.environ, "NOSTR_SECRET_KEY": nsec}, timeout=90,
        )
    except subprocess.TimeoutExpired:
        log.error("publish timed out for kind %d %s", ev.kind, ev.d_tag or ev.title[:40])
        return False
    if result.returncode != 0:
        log.error("publish failed for kind %d %s: %s", ev.kind, ev.d_tag, result.stderr.strip())
        return False
    log.info("published kind %d %s (%s)", ev.kind, ev.d_tag or "", ev.title[:60])
    return True


def publish_deletion(pubkey: str, d_tag: str, *, nsec: str) -> bool:
    ev = NostrEvent(
        kind=5, d_tag=None, title=d_tag,
        tags=[("a", f"31923:{pubkey}:{d_tag}"), ("k", "31923")],
        content="Meetup removed from the Omarchy Meetups calendar on Luma.",
    )
    return publish_event(ev, nsec=nsec)


# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            log.warning("state.json is corrupt, starting fresh")
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------

def collect_meetups(*, limit: int = 0, details: bool = True) -> list[Meetup]:
    entries = fetch_calendar("future")
    log.info("Luma returned %d upcoming entries", len(entries))
    if limit:
        entries = entries[:limit]
    meetups: list[Meetup] = []
    for entry in entries:
        api_id = entry["event"]["api_id"]
        detail = None
        if details:
            try:
                detail = fetch_event_detail(api_id)
                time.sleep(0.3)
            except Exception as e:
                log.warning("no detail for %s (%s); publishing without description", api_id, e)
        try:
            meetups.append(build_meetup(entry, detail))
        except Exception:
            log.exception("failed to build meetup %s", api_id)
    return meetups


def cmd_sync(*, dry_run: bool, limit: int, announce: bool, announce_limit: int,
             sweep: bool, force_sweep: bool) -> int:
    nsec = "" if dry_run else load_nsec()
    pubkey = "<pubkey>" if dry_run else pubkey_from_nsec(nsec)
    state = {} if dry_run else load_state()
    now = int(time.time())

    meetups = collect_meetups(limit=limit)
    if not meetups:
        log.error("no meetups parsed; refusing to sweep or publish")
        return 1

    n_pub = n_skip = n_fail = n_ann = n_del = 0
    new_meetups: list[Meetup] = []

    for m in meetups:
        ev = build_calendar_event(m)
        h = ev.canonical_hash()
        prev = state.get(ev.d_tag)
        if dry_run:
            _dump_event(ev)
            continue
        if prev and prev.get("hash") == h and not prev.get("deleted"):
            n_skip += 1
            continue
        if publish_event(ev, nsec=nsec):
            if not prev:
                new_meetups.append(m)
            state[ev.d_tag] = {
                "hash": h, "kind": ev.kind, "start": int(m.start.timestamp()),
                "title": m.name, "url": m.url,
                "last_published": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            n_pub += 1
        else:
            n_fail += 1

    # Calendar (kind 31924) listing every upcoming meetup
    cal = build_calendar(pubkey, meetups)
    if dry_run:
        _dump_event(cal)
    else:
        h = cal.canonical_hash()
        prev = state.get("__calendar__")
        if not prev or prev.get("hash") != h:
            if publish_event(cal, nsec=nsec):
                state["__calendar__"] = {"hash": h, "kind": 31924,
                                         "last_published": dt.datetime.now(dt.timezone.utc).isoformat()}
                n_pub += 1
            else:
                n_fail += 1

    # Sweep: upcoming meetups we published that left the calendar
    if sweep and not dry_run and not limit:
        live = {m.d_tag for m in meetups}
        gone = [d for d, rec in state.items()
                if d.startswith("luma-") and d not in live and not rec.get("deleted")
                and rec.get("start", 0) > now]
        if len(gone) > MAX_SWEEP and not force_sweep:
            log.warning("%d upcoming meetups vanished from Luma; skipping sweep (use --force-sweep)", len(gone))
        else:
            for d in gone:
                if publish_deletion(pubkey, d, nsec=nsec):
                    state[d]["deleted"] = True
                    n_del += 1
                else:
                    n_fail += 1

    # Announcements for brand-new meetups (capped so a first seed doesn't flood)
    if announce and not dry_run:
        for m in sorted(new_meetups, key=lambda x: x.start)[:announce_limit]:
            try:
                naddr = encode_naddr(pubkey, 31923, m.d_tag)
                if publish_event(build_announcement(m, naddr), nsec=nsec):
                    n_ann += 1
            except Exception:
                log.exception("announcement failed for %s", m.d_tag)
                n_fail += 1

    if not dry_run:
        save_state(state)
    log.info("sync complete: published=%d skipped=%d deleted=%d announced=%d failed=%d",
             n_pub, n_skip, n_del, n_ann, n_fail)
    return 0 if n_fail == 0 else 1


def _dump_event(ev: NostrEvent) -> None:
    obj = {
        "kind": ev.kind, "d": ev.d_tag,
        "tags": [list(t) for t in ev.tags],
        "content_preview": ev.content[:300] + ("..." if len(ev.content) > 300 else ""),
    }
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_fetch(*, limit: int, details: bool) -> int:
    meetups = collect_meetups(limit=limit, details=details)
    for m in sorted(meetups, key=lambda x: x.start):
        when = m.local_start().strftime("%Y-%m-%d %H:%M %Z")
        where = m.location or "(no location)"
        print(f"{when:<22} {m.name[:48]:<48} {where[:60]}")
        if details:
            print(f"{'':22} hosts={', '.join(m.hosts) or '-'}  cover={'yes' if m.cover else 'no'}  "
                  f"desc={len(m.description)} chars  g={'yes' if m.latlng else 'no'}  {m.url}")
    print(f"\n{len(meetups)} upcoming meetups")
    return 0


def cmd_publish_profile(*, dry_run: bool = False) -> int:
    profile = {k: v for k, v in PROFILE.items() if v}
    content = json.dumps(profile, ensure_ascii=False)
    if dry_run:
        print(json.dumps(profile, indent=2, ensure_ascii=False))
        return 0
    nsec = load_nsec()
    cmd = ["nak", "event", "-k", "0", "-c", content, *RELAYS]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            env={**os.environ, "NOSTR_SECRET_KEY": nsec}, timeout=90)
    if result.returncode != 0:
        log.error("profile publish failed: %s", result.stderr)
        return 1
    log.info("published profile metadata (kind 0)")
    return 0


def self_test() -> int:
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Hello "},
                                          {"type": "text", "text": "world", "marks": [{"type": "bold"}]}]},
        {"type": "bulletList", "content": [
            {"type": "list_item", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"},
                                                                                {"type": "hard_break"},
                                                                                {"type": "text", "text": "two"}]}]},
            {"type": "listItem", "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": "site", "marks": [{"type": "link", "attrs": {"href": "https://x.y"}}]}]}]},
        ]},
    ]}
    out = prosemirror_to_text(doc)
    assert out == "Hello world\n\n- one\ntwo\n- site (https://x.y)", repr(out)
    assert geohash(36.175983, -86.7545616, 9).startswith("dn6m")
    assert slugify_tag("Hlavní město Praha") == "hlavnimestopraha"
    assert slugify_tag("Bakı") == "baki"
    assert first_sentence("A short one.\n\nMore.") == "A short one."
    loc, ll, prec, _ = resolve_location({"location_type": "offline", "geo_address_visibility": "guests-only",
                                         "geo_address_info": {"city": "Dubai", "country": "UAE", "city_state": None},
                                         "coordinate": {"latitude": 25.2, "longitude": 55.3}})
    assert loc == "Dubai, UAE" and prec == 5 and ll is not None, (loc, prec)
    loc, ll, prec, _ = resolve_location({"location_type": "meet"})
    assert loc.startswith("Online") and ll is None
    print("self-test OK")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="omarchybot", description=__doc__)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--self-test", action="store_true", dest="self_test")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("sync", help="mirror upcoming meetups to Nostr (what the timer runs)")
    sp.add_argument("--dry-run", action="store_true", help="build and print events, publish nothing")
    sp.add_argument("--limit", type=int, default=0, help="only the first N entries (disables sweep)")
    sp.add_argument("--announce", action="store_true", help="also post a kind 1 note for new meetups")
    sp.add_argument("--announce-limit", type=int, default=ANNOUNCE_LIMIT)
    sp.add_argument("--no-sweep", action="store_true", help="don't delete meetups that left the calendar")
    sp.add_argument("--force-sweep", action="store_true", help=f"allow more than {MAX_SWEEP} deletions")

    fp = sub.add_parser("fetch", help="print parsed meetups, touch nothing on Nostr")
    fp.add_argument("--limit", type=int, default=0)
    fp.add_argument("--no-details", action="store_true", help="skip per-event detail requests")

    pp = sub.add_parser("publish-profile", help="(re)publish kind 0 metadata")
    pp.add_argument("--dry-run", action="store_true", help="print the profile JSON, publish nothing")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.self_test:
        return self_test()
    if args.cmd == "sync":
        return cmd_sync(dry_run=args.dry_run, limit=args.limit, announce=args.announce,
                        announce_limit=args.announce_limit, sweep=not args.no_sweep,
                        force_sweep=args.force_sweep)
    if args.cmd == "fetch":
        return cmd_fetch(limit=args.limit, details=not args.no_details)
    if args.cmd == "publish-profile":
        return cmd_publish_profile(dry_run=args.dry_run)
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
