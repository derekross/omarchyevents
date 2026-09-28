# omarchyevents

Cross-poster that mirrors the **Omarchy Meetups** calendar on Luma
([luma.com/omarchy](https://luma.com/omarchy), the calendar behind
[omarchy.org/meetups](https://omarchy.org/meetups/)) to [Nostr](https://nostr.com) as
[NIP-52](https://github.com/nostr-protocol/nips/blob/master/52.md) calendar events.

A small Python service polls Luma hourly, fetches each meetup's details, and republishes them
to a set of Nostr relays under a dedicated **Omarchy Events** identity. Any client that renders
calendar events (Ditto, Coracle, Flockstr, …) can show them, and one kind 31924 calendar event
groups every upcoming meetup.

Unofficial community mirror. Not affiliated with Omarchy or the Omacom Foundation. Meetups are
community-run; RSVP happens on Luma.

Sibling of [raystownevents](https://github.com/derekross/raystownevents), with the RSS scraping
and time parsing replaced by Luma's JSON.

## What gets published

- **kind 0**: profile metadata (`Omarchy Events`, about, website, picture)
- **kind 31923**: one time-based calendar event per upcoming meetup
- **kind 31924**: the `Omarchy Meetups` calendar, an `a` tag per upcoming meetup
- **kind 5** (sweep): a deletion when an upcoming meetup disappears from the Luma calendar
- **kind 1** (optional, `--announce`): a short note per newly listed meetup with its `nostr:naddr`

Tags on each kind 31923:

| tag | example |
| --- | --- |
| `d` | `luma-evt-CDLEcZNMJtilaTP` |
| `title` | `Omarchy Nashville Meetup` |
| `start` / `end` | unix seconds |
| `start_tzid` / `end_tzid` | `America/Chicago` (Luma's per-event timezone) |
| `summary` | first paragraph of the description, trimmed to 200 chars |
| `image` | Luma cover image |
| `location` | `Up-Down Nashville, 927 Woodland St, Nashville, TN 37206, USA` |
| `g` | geohash of the venue |
| `t` | `omarchy`, `omarchymeetup`, `linux`, `meetup`, city slug, country code |
| `r` | `https://luma.com/de7na6qn` (RSVP) |

Content is the description as plain text, then `Hosted by …`, the RSVP link and the calendar link.

**Address privacy.** Luma hosts can hide the venue until RSVP (`guests-only`). Those meetups get
the city only (`Dubai, United Arab Emirates`) and a 5-character geohash (~5 km), never the venue.
Online meetups get `Online (link on Luma after RSVP)` and no geohash.

## Default relays

```
wss://relay.ditto.pub
wss://relay.damus.io
wss://nos.lol
wss://relay.primal.net
```

Edit `RELAYS` in `omarchybot.py` to change.

## Install

Requires Python 3.11+, [`nak`](https://github.com/fiatjaf/nak), and `systemd` (Linux).

```sh
git clone https://github.com/derekross/omarchyevents.git ~/projects/omarchyevents
cd ~/projects/omarchyevents
./setup.sh
```

The systemd unit expects the checkout at `~/projects/omarchyevents` (edit `WorkingDirectory`
and `ExecStart` in `systemd/omarchyevents.service` for another path).

The installer creates a `.venv`, generates an `nsec` at `~/.config/omarchyevents/nsec`
(chmod 600) if none exists, runs the self-test, publishes the kind 0 profile, installs the
user-mode systemd units and enables the hourly timer. First sync runs ~2 minutes later.

The profile picture is `assets/avatar.svg` rendered to PNG and uploaded to Blossom with the bot's
key; set `OMARCHYEVENTS_PICTURE` to override. It is not the Omarchy logo, which is trademarked.

## CLI

```sh
.venv/bin/python omarchybot.py fetch                       # parsed meetups, touches nothing
.venv/bin/python omarchybot.py fetch --limit 5 --no-details
.venv/bin/python omarchybot.py sync --dry-run              # build events, print, publish nothing
.venv/bin/python omarchybot.py sync                        # what the timer runs
.venv/bin/python omarchybot.py sync --announce             # also post kind 1 for new meetups (3/run)
.venv/bin/python omarchybot.py sync --no-sweep             # never delete
.venv/bin/python omarchybot.py publish-profile             # (re)publish kind 0
.venv/bin/python omarchybot.py --self-test
```

## How it works

1. **Discover.** `GET https://api.lu.ma/calendar/get-items?calendar_api_id=cal-SDGGMsEps9ExsrT&period=future`
   lists every upcoming meetup with times, timezone, cover, address, coordinates and hosts.
2. **Detail.** `GET https://api.lu.ma/event/get?event_api_id=evt-…` for the description, which Luma
   stores as a ProseMirror document. It's flattened to plain text with simple markdown lists.
3. **Build.** Each meetup becomes a kind 31923 with a stable `d` tag; all of them go into one
   kind 31924 calendar.
4. **Dedupe.** A canonical hash (kind + d + sorted tags + content) is compared against
   `~/.config/omarchyevents/state.json`. Unchanged events are skipped.
5. **Publish.** Changed events are signed and broadcast via `nak event`. Addressable kinds replace
   cleanly on relays.
6. **Sweep.** An upcoming meetup we published that is no longer on the calendar gets a kind 5
   deletion. If more than 10 vanish at once the sweep is skipped (probably an API hiccup); pass
   `--force-sweep` to override.

## Operations

```sh
systemctl --user status omarchyevents.timer
journalctl --user -u omarchyevents.service --since '1 hour ago'
systemctl --user start omarchyevents.service     # run now
```

## Known limitations

- **Luma's API is unofficial.** The bot fails loudly (non-zero exit, nothing published or swept)
  if the payload shape changes. The iCal feed at `api.lu.ma/ics/get?entity=calendar&id=…` exists
  but lacks addresses and descriptions, so it isn't used as a fallback: publishing degraded
  events would overwrite good ones.
- **Damus's strfry** filters events from brand-new pubkeys for a while.
- **Hosts aren't Nostr pubkeys**, so there are no `p` tags. Hosts are named in the content.
- **Recurring meetups** arrive from Luma as separate entries, one per occurrence, so nothing is
  expanded here. Past occurrences are left on relays as history.

## Files

| path | purpose |
| --- | --- |
| `omarchybot.py` | single-file CLI |
| `requirements.txt` | `requests` |
| `setup.sh` | idempotent installer |
| `systemd/omarchyevents.{service,timer}` | user-mode systemd units |
| `assets/avatar.svg` | profile picture source (Tokyo Night palette, no Omarchy logo) |
| `~/.config/omarchyevents/nsec` | secret key (chmod 600, never committed) |
| `~/.config/omarchyevents/state.json` | content-hash dedupe state |
