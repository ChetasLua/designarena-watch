#!/usr/bin/env python3
"""Design Arena stealth-model watcher -> Discord.

Polls https://www.designarena.ai/api/registry (the public JSON every visitor's
browser loads) and posts a Discord webhook alert when the stealth roster changes.

How Design Arena handles stealth models (observed Mar-Oct 2026):
  1. A codename gets a provider row {"displayName": <codename>, "logoName": "private.png"}
     while its model row is held back from `models`.
  2. On reveal a model row appears whose id == the codename, with the real lab
     and display name (hampshire -> MiMo-V2.6-Pro, pelican -> Canva Craft, ...).
  3. Since late Sep 2026 the codename provider row is then deleted.

Events: stealth_added, stealth_revealed, stealth_removed, stealth_changed,
model_added, model_removed, model_changed, provider_added.

Stdlib only (Python 3.9+). Usage:
  python3 watch.py once                 # one poll; first run just records a baseline
  python3 watch.py loop --interval 120  # poll forever
  python3 watch.py status               # current unrevealed stealth slots
  python3 watch.py baseline --post      # post the current stealth roster to Discord
  python3 watch.py replay DIR           # replay saved registry snapshots (test)
  python3 watch.py test-post            # send one test message to the webhook
Add --dry-run to print Discord payloads instead of sending them.
Webhook: env DA_WEBHOOK_URL, or "webhook_url" in config.json next to this file.
"""

import argparse
import datetime as dt
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
REGISTRY_URL = "https://www.designarena.ai/api/registry"
SITE = "https://www.designarena.ai"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36 designarena-watch/1.0")
PRIVATE_LOGO = "private.png"

COLORS = {
    "stealth_added": 0x8B5CF6,
    "stealth_revealed": 0xF59E0B,
    "stealth_removed": 0x6B7280,
    "stealth_changed": 0xA78BFA,
    "model_added": 0x10B981,
    "model_removed": 0xEF4444,
    "model_changed": 0x3B82F6,
    "provider_added": 0x14B8A6,
    "baseline": 0x8B5CF6,
}
ICONS = {
    "stealth_added": "🕵️",
    "stealth_revealed": "🎭",
    "stealth_removed": "🫥",
    "stealth_changed": "🔁",
    "model_added": "🆕",
    "model_removed": "❌",
    "model_changed": "✏️",
    "provider_added": "🏢",
}

# Name families seen in Design Arena codenames. Labels only, not attribution.
FAMILIES = {
    "trees/wood": {"maple", "aspen", "cypress", "redwood", "spruce", "sequoia", "hemlock", "larch",
                   "plywood", "willow", "cedar", "oak", "birch", "juniper", "magnolia", "evergreen",
                   "chestnut", "hazel", "hazelnut", "pine", "elm", "fir", "ash", "alder", "teak",
                   "mahogany", "walnut", "poplar", "sycamore", "beech", "linden", "baobab", "balsa"},
    "places (US states)": {"vermont", "hampshire", "oregon", "dakota", "hawaii", "arkansas",
                           "new-mexico", "colorado", "nevada", "montana", "idaho", "maine", "ohio",
                           "utah", "alaska", "texas", "kansas", "york", "virginia", "carolina"},
    "places (Canada)": {"manitoba", "montreal", "halifax", "ontario", "quebec", "toronto",
                        "vancouver", "calgary", "ottawa", "winnipeg", "alberta", "yukon"},
    "elements": {"lithium", "argon", "krypton", "xenon", "radon", "zinc", "magnesium", "neon",
                 "helium", "carbon", "barium", "cobalt", "titanium", "iridium", "osmium"},
    "space": {"venus", "mercury", "mars", "neptune", "pluto", "jupiter", "saturn", "andromeda",
              "callisto", "vela", "comet", "nova", "alpha_centauri", "orion", "lyra"},
    "birds": {"robin", "cardinal", "sparrow", "bluebird", "swan", "kestrel", "raven", "bluejay",
              "pelican", "emperor", "rockhopper", "macaroni", "mumble"},
    "coffee": {"coffee", "latte", "cortado", "ristretto", "caffeine", "decaf", "vanilla"},
    "flowers": {"poppy", "clover", "marigold", "dahlia", "lupine", "zinnia", "begonia", "camellia",
                "lotus", "carnation", "thistle", "lavender", "violet", "cherryblossom", "wildflower"},
    "gems": {"obsidian", "pearl", "quartz", "sapphire", "ruby", "amber", "marble", "prism"},
}

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------- helpers

def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"[{iso(utcnow())}] {msg}", flush=True)


def family_of(name):
    base = name.lower()
    stem = re.sub(r"[-_]?\d+$", "", base)
    for fam, words in FAMILIES.items():
        if base in words or stem in words:
            return fam
    return None


def date_hint(name):
    """lithium-1015 -> 'suffix 1015 looks like Oct 15'."""
    m = re.search(r"[-_](\d{2})(\d{2})$", name)
    if not m:
        return None
    mm, dd = int(m.group(1)), int(m.group(2))
    if 1 <= mm <= 12 and 1 <= dd <= 31:
        return f"suffix {m.group(1)}{m.group(2)} looks like {MONTHS[mm - 1]} {dd}"
    return None


def stealth_slots(reg):
    """Provider keys that are codename slots: private logo, no model row uses them,
    and no model has the codename as its id yet."""
    used = {m.get("provider") for m in reg["models"].values()}
    return {k: v for k, v in reg["providers"].items()
            if v.get("logoName") == PRIVATE_LOGO and k not in used and k not in reg["models"]}


def model_summary(reg, mid):
    m = reg["models"][mid]
    prov = reg["providers"].get(m.get("provider"), {})
    arenas = (m.get("arenas") or {}).get("models") or []
    price = (reg.get("pricing") or {}).get(mid) or {}
    return {
        "id": mid,
        "name": m.get("displayName", mid),
        "lab": prov.get("displayName") or m.get("provider"),
        "provider": m.get("provider"),
        "active": m.get("active"),
        "openSource": m.get("openSource"),
        "arenas": arenas,
        "price": price,
    }


def fmt_price(price):
    parts = []
    for kind, p in (price or {}).items():
        if isinstance(p, dict) and ("input" in p or "output" in p):
            parts.append(f"{kind} ${p.get('input', '?')}/${p.get('output', '?')} per M")
        elif isinstance(p, dict):
            parts.append(f"{kind} " + ", ".join(f"{k} {v}" for k, v in p.items()))
        else:
            parts.append(f"{kind} {p}")
    return "; ".join(parts) or "—"


# ---------------------------------------------------------------- fetch / state

def fetch_registry(url=REGISTRY_URL, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json",
                                                       "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
            reg = json.loads(body)
            validate(reg)
            return reg
        except Exception as e:  # network blips, 5xx, bad JSON
            last = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"registry fetch failed after {tries} tries: {last}")


def validate(reg):
    if not isinstance(reg, dict) or not isinstance(reg.get("models"), dict) \
            or not isinstance(reg.get("providers"), dict):
        raise ValueError("unexpected registry shape")
    if len(reg["models"]) < 100 or len(reg["providers"]) < 50:
        raise ValueError(f"registry looks truncated ({len(reg['models'])} models)")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_config():
    cfg = load_json(os.path.join(HERE, "config.json"), {})
    cfg.setdefault("webhook_url", os.environ.get("DA_WEBHOOK_URL", ""))
    cfg.setdefault("mention_role_id", os.environ.get("DA_MENTION_ROLE_ID", ""))
    cfg.setdefault("mention_on", ["stealth_added", "stealth_revealed"])
    cfg.setdefault("mute", [])
    cfg.setdefault("mute_fields", ["pricing"])  # model_changed fields to ignore
    cfg.setdefault("digest_over", 4)  # more model_changed than this -> one digest embed
    cfg.setdefault("username", "Design Arena Watch")
    return cfg


# ---------------------------------------------------------------- diff engine

def diff(old, new, now, first_seen):
    """Compare two registries; return events. Updates first_seen in place."""
    ev = []
    t = iso(now)
    old_slots, new_slots = stealth_slots(old), stealth_slots(new)
    om, nm = old["models"], new["models"]
    added_models = [k for k in nm if k not in om]
    removed_models = [k for k in om if k not in nm]

    # stealth slots that disappeared: revealed (model id == codename) or just removed
    revealed_ids = set()
    for code in old_slots:
        if code in new_slots:
            continue
        if code in nm:
            revealed_ids.add(code)
            s = model_summary(new, code)
            ev.append({"type": "stealth_revealed", "codename": code, "model": s,
                       "first_seen": first_seen.get("p:" + code), "at": t})
        elif code not in new["providers"]:
            ev.append({"type": "stealth_removed", "codename": code,
                       "first_seen": first_seen.get("p:" + code),
                       "same_poll_new_models": [model_summary(new, k) for k in added_models],
                       "at": t})
        else:
            # row still there but now has a real logo or a model using it -> lab revealed
            ev.append({"type": "stealth_changed", "codename": code,
                       "old": old["providers"][code], "new": new["providers"][code],
                       "models": [model_summary(new, k) for k, m in nm.items()
                                  if m.get("provider") == code],
                       "first_seen": first_seen.get("p:" + code), "at": t})

    # brand-new stealth slots
    fresh = [code for code in new_slots if code not in old_slots and code not in old["providers"]]
    for code in fresh:
        first_seen.setdefault("p:" + code, t)
        fam = family_of(code)
        sib = sorted(c for c in new_slots if c != code and fam and family_of(c) == fam)
        ev.append({"type": "stealth_added", "codename": code,
                   "displayName": new_slots[code].get("displayName", code),
                   "family": fam, "family_siblings": sib, "date_hint": date_hint(code),
                   "batch": sorted(fresh), "at": t})

    # stealth slot display name edits (still private)
    for code in new_slots:
        if code in old_slots and old_slots[code] != new_slots[code]:
            ev.append({"type": "stealth_changed", "codename": code, "old": old_slots[code],
                       "new": new_slots[code], "models": [], "first_seen": first_seen.get("p:" + code),
                       "at": t})

    # models
    for k in added_models:
        first_seen.setdefault("m:" + k, t)
        if k in revealed_ids:
            continue
        ev.append({"type": "model_added", "model": model_summary(new, k), "at": t})
    for k in removed_models:
        ev.append({"type": "model_removed", "model": model_summary(old, k), "at": t})
    for k in nm:
        if k not in om:
            continue
        a, b = om[k], nm[k]
        changes = {}
        for field in ("displayName", "provider", "active", "openSource", "router"):
            if a.get(field) != b.get(field):
                changes[field] = [a.get(field), b.get(field)]
        aa = set((a.get("arenas") or {}).get("models") or [])
        ba = set((b.get("arenas") or {}).get("models") or [])
        if aa != ba:
            changes["arenas"] = {"added": sorted(ba - aa), "removed": sorted(aa - ba)}
        op, np_ = (old.get("pricing") or {}).get(k), (new.get("pricing") or {}).get(k)
        if op != np_:
            changes["pricing"] = [op, np_]
        if changes:
            ev.append({"type": "model_changed", "model": model_summary(new, k),
                       "changes": changes, "at": t})

    # new real (non-private) providers = new lab on the arena
    for k, v in new["providers"].items():
        if k not in old["providers"] and v.get("logoName") != PRIVATE_LOGO:
            first_seen.setdefault("p:" + k, t)
            ev.append({"type": "provider_added", "provider": k, "displayName": v.get("displayName"),
                       "website": v.get("website"), "at": t})
    return ev


# ---------------------------------------------------------------- discord formatting

def _model_line(s):
    lab = f" · {s['lab']}" if s.get("lab") else ""
    return f"**{s['name']}**{lab} (`{s['id']}`)"


def embed_for(e):
    typ = e["type"]
    icon = ICONS.get(typ, "•")
    fields = []
    if typ == "stealth_added":
        title = f"{icon} New stealth model: `{e['codename']}`"
        desc = ("A new hidden codename slot appeared in Design Arena's public registry "
                "(private logo, no model row yet).")
        if e.get("family"):
            sib = ", ".join(e["family_siblings"][:20]) or "none"
            fields.append({"name": "Family", "value": f"{e['family']} — other live slots: {sib}",
                           "inline": False})
        if e.get("date_hint"):
            fields.append({"name": "Hint", "value": e["date_hint"], "inline": True})
        if len(e.get("batch", [])) > 1:
            fields.append({"name": "Added in the same poll",
                           "value": ", ".join(f"`{c}`" for c in e["batch"]), "inline": False})
    elif typ == "stealth_revealed":
        s = e["model"]
        title = f"{icon} Revealed: `{e['codename']}` → {s['name']}"
        desc = f"{_model_line(s)}"
        fields.append({"name": "Lab", "value": str(s.get("lab") or "?"), "inline": True})
        fields.append({"name": "Open weights", "value": str(s.get("openSource")), "inline": True})
        if s.get("arenas"):
            fields.append({"name": "Arenas", "value": ", ".join(s["arenas"]), "inline": False})
        if s.get("price"):
            fields.append({"name": "Price", "value": fmt_price(s["price"]), "inline": False})
    elif typ == "stealth_removed":
        title = f"{icon} Stealth slot removed: `{e['codename']}`"
        desc = "The codename row was deleted without a model under that id."
        cands = e.get("same_poll_new_models") or []
        if cands:
            fields.append({"name": "Models that appeared in the same poll (possible match)",
                           "value": "\n".join(_model_line(s) for s in cands[:8]), "inline": False})
    elif typ == "stealth_changed":
        title = f"{icon} Stealth slot changed: `{e['codename']}`"
        desc = f"`{json.dumps(e['old'])}`\n→ `{json.dumps(e['new'])}`"
        if e.get("models"):
            fields.append({"name": "Models now under it",
                           "value": "\n".join(_model_line(s) for s in e["models"][:8]), "inline": False})
    elif typ == "model_added":
        s = e["model"]
        title = f"{icon} New model: {s['name']}"
        desc = _model_line(s)
        if s.get("arenas"):
            fields.append({"name": "Arenas", "value": ", ".join(s["arenas"]), "inline": False})
        if s.get("price"):
            fields.append({"name": "Price", "value": fmt_price(s["price"]), "inline": False})
        fields.append({"name": "Active", "value": str(s.get("active")), "inline": True})
    elif typ == "model_removed":
        s = e["model"]
        title = f"{icon} Model removed: {s['name']}"
        desc = _model_line(s)
    elif typ == "model_changed":
        s = e["model"]
        title = f"{icon} Model changed: {s['name']}"
        desc = _model_line(s)
        for k, v in e["changes"].items():
            if k == "arenas":
                val = (f"+ {', '.join(v['added'])}" if v["added"] else "") + \
                      (f"  − {', '.join(v['removed'])}" if v["removed"] else "")
            elif k == "pricing":
                val = f"{fmt_price(v[0])} → {fmt_price(v[1])}"
            else:
                val = f"{v[0]} → {v[1]}"
            fields.append({"name": k, "value": val[:1000] or "—", "inline": False})
    elif typ == "provider_added":
        title = f"{icon} New lab on Design Arena: {e.get('displayName')}"
        desc = f"`{e['provider']}`" + (f" · {e['website']}" if e.get("website") else "")
    else:
        title, desc = typ, json.dumps(e)[:1000]
    if e.get("first_seen") and typ.startswith("stealth_") and typ != "stealth_added":
        fields.append({"name": "Slot first seen", "value": e["first_seen"], "inline": True})
    return {"title": title[:256], "description": desc[:2000], "color": COLORS.get(typ, 0x999999),
            "fields": fields[:25], "url": SITE + "/leaderboard",
            "footer": {"text": "designarena.ai/api/registry"}, "timestamp": e["at"]}


def _change_text(k, v):
    if k == "arenas":
        return " ".join(x for x in (f"+{','.join(v['added'])}" if v["added"] else "",
                                    f"−{','.join(v['removed'])}" if v["removed"] else "") if x)
    if k == "pricing":
        return f"{fmt_price(v[0])} → {fmt_price(v[1])}"
    return f"{v[0]} → {v[1]}"


def digest_embed(changed, at):
    lines = []
    for e in changed:
        s = e["model"]
        lines.append(f"**{s['name']}** · " + "; ".join(f"{k}: {_change_text(k, v)}"
                                                       for k, v in e["changes"].items()))
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900].rsplit("\n", 1)[0] + f"\n… and more ({len(lines)} total)"
    return {"title": f"✏️ {len(changed)} model rows changed", "description": text,
            "color": COLORS["model_changed"], "url": SITE + "/leaderboard",
            "footer": {"text": "designarena.ai/api/registry"}, "timestamp": at}


def payloads_for(events, cfg):
    events = [e for e in events if e["type"] not in cfg.get("mute", [])]
    muted_fields = set(cfg.get("mute_fields", []))
    kept = []
    for e in events:
        if e["type"] == "model_changed" and muted_fields:
            ch = {k: v for k, v in e["changes"].items() if k not in muted_fields}
            if not ch:
                continue
            e = dict(e, changes=ch)
        kept.append(e)
    events = kept
    if not events:
        return []
    order = ["stealth_revealed", "stealth_added", "stealth_changed", "stealth_removed",
             "model_added", "provider_added", "model_changed", "model_removed"]
    events.sort(key=lambda e: order.index(e["type"]) if e["type"] in order else 99)
    changed = [e for e in events if e["type"] == "model_changed"]
    if len(changed) > cfg.get("digest_over", 4):
        embeds = [embed_for(e) for e in events if e["type"] != "model_changed"]
        embeds.append(digest_embed(changed, changed[0]["at"]))
    else:
        embeds = [embed_for(e) for e in events]
    mention = ""
    if cfg.get("mention_role_id") and any(e["type"] in cfg["mention_on"] for e in events):
        mention = f"<@&{cfg['mention_role_id']}>"
    out, chunk, size = [], [], 0
    for em in embeds:
        n = len(json.dumps(em))
        if chunk and (len(chunk) == 10 or size + n > 5500):
            out.append(chunk)
            chunk, size = [], 0
        chunk.append(em)
        size += n
    if chunk:
        out.append(chunk)
    payloads = []
    for i, ch in enumerate(out):
        p = {"username": cfg["username"], "embeds": ch, "allowed_mentions": {"parse": [], "roles": []}}
        if i == 0 and mention:
            p["content"] = mention
            p["allowed_mentions"]["roles"] = [cfg["mention_role_id"]]
        payloads.append(p)
    return payloads


def post(cfg, payloads, dry_run=False):
    if not payloads:
        return True
    if dry_run or not cfg.get("webhook_url"):
        for p in payloads:
            print(json.dumps(p, indent=1, ensure_ascii=False))
        if not dry_run:
            log("no webhook configured; payloads kept in pending.json until one is set")
            return False
        return True
    ok = True
    for p in payloads:
        data = json.dumps(p).encode()
        for attempt in range(5):
            req = urllib.request.Request(cfg["webhook_url"], data=data, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=30):
                    break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    try:
                        wait = float(json.loads(e.read() or b"{}").get("retry_after", 2))
                    except Exception:
                        wait = 2.0
                    time.sleep(min(wait, 60) + 0.5)
                    continue
                log(f"webhook HTTP {e.code}: {e.read()[:300]!r}")
                ok = False
                break
            except Exception as e:
                log(f"webhook error: {e}")
                time.sleep(3 * (attempt + 1))
        else:
            ok = False
        time.sleep(1.1)  # stay under Discord's webhook rate limit
    return ok


# ---------------------------------------------------------------- commands

def paths():
    os.makedirs(os.path.join(STATE_DIR, "snapshots"), exist_ok=True)
    return (os.path.join(STATE_DIR, "registry.json"), os.path.join(STATE_DIR, "first_seen.json"),
            os.path.join(STATE_DIR, "events.jsonl"), os.path.join(STATE_DIR, "pending.json"))


def snapshot(reg, now):
    if os.environ.get("DA_NO_SNAPSHOTS"):  # e.g. in GitHub Actions, where git keeps history
        return
    p = os.path.join(STATE_DIR, "snapshots", now.strftime("%Y%m%dT%H%M%SZ") + ".json.gz")
    with gzip.open(p, "wt") as f:
        json.dump(reg, f)


def poll_once(cfg, dry_run=False):
    reg_p, fs_p, ev_p, pend_p = paths()
    now = utcnow()
    new = fetch_registry()
    old = load_json(reg_p, None)
    first_seen = load_json(fs_p, {})
    if old is None:
        for code in stealth_slots(new):
            first_seen.setdefault("p:" + code, "before " + iso(now))
        save_json(reg_p, new)
        save_json(fs_p, first_seen)
        snapshot(new, now)
        log(f"baseline saved: {len(new['models'])} models, {len(new['providers'])} providers, "
            f"{len(stealth_slots(new))} stealth slots. Nothing posted.")
        return []
    events = diff(old, new, now, first_seen)
    pending = load_json(pend_p, [])
    if events:
        snapshot(new, now)
        with open(ev_p, "a") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
    to_send = pending + payloads_for(events, cfg)
    if to_send:
        if post(cfg, to_send, dry_run=dry_run):
            pending = []
        else:
            pending = to_send[-50:]  # retry next poll, keep it bounded
            log(f"{len(pending)} payload(s) queued for retry")
    save_json(pend_p, pending)
    save_json(reg_p, new)
    save_json(fs_p, first_seen)
    log(f"poll ok: {len(events)} event(s) " + ", ".join(sorted({e['type'] for e in events})))
    return events


def cmd_loop(cfg, interval, dry_run):
    interval = max(60, interval)
    while True:
        try:
            poll_once(cfg, dry_run)
        except Exception as e:
            log(f"poll failed: {e}")
        time.sleep(interval)


def roster_embed(codes, first_seen, total, since):
    lines = []
    for c in codes:
        extra = " · ".join(x for x in (family_of(c), date_hint(c)) if x)
        seen = first_seen.get("p:" + c, "?").replace("T", " ").replace("Z", "")
        lines.append(f"`{c}` — first seen {seen}" + (f" · {extra}" if extra else ""))
    desc = ("Hidden codename slots in Design Arena's public `/api/registry` "
            "(private logo, no model row yet)" + (f", first seen after {since}" if since else "") +
            f". {total} slots exist in total; older ones are mostly stale.\n\n" + "\n".join(lines))
    if len(desc) > 4000:
        desc = desc[:4000].rsplit("\n", 1)[0] + "\n…"
    return {"title": f"🕵️ Design Arena: {len(codes)} unrevealed stealth models",
            "description": desc, "color": COLORS["baseline"], "url": SITE + "/leaderboard",
            "footer": {"text": "designarena.ai/api/registry"}, "timestamp": iso(utcnow())}


def cmd_status(post_it, cfg, dry_run, only, since):
    reg_p, fs_p, _, _ = paths()
    reg = load_json(reg_p, None) or fetch_registry()
    fs = load_json(fs_p, {})
    slots = stealth_slots(reg)
    codes = sorted(slots, key=lambda c: (_seen_end(fs.get("p:" + c)), c), reverse=True)
    if only:
        codes = [c for c in codes if c in set(only)]
    if since:
        codes = [c for c in codes if _seen_end(fs.get("p:" + c)) >= since]
    print(f"{len(codes)} of {len(slots)} stealth slots (private logo, no model row), newest first:")
    for c in codes:
        extra = " · ".join(x for x in (family_of(c), date_hint(c)) if x)
        print(f"  {c:16s} first seen {str(fs.get('p:' + c, '?')):44s} {extra}")
    if post_it:
        post(cfg, [{"username": cfg["username"], "embeds": [roster_embed(codes, fs, len(slots), since)],
                    "allowed_mentions": {"parse": []}}], dry_run=dry_run)


def _snapshot_series(directory):
    out = []
    for f in sorted(os.listdir(directory)):
        m = re.search(r"(\d{14})", f)
        if not m or not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, f)) as fh:
                reg = json.load(fh)
            validate(reg)
        except Exception:
            continue
        t = dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
        out.append((t, reg))
    return out


def cmd_seed(directory):
    """Backfill first-seen windows for current stealth slots from dated registry snapshots
    (e.g. Wayback copies named reg_YYYYmmddHHMMSS.json)."""
    _, fs_p, _, _ = paths()
    fs = load_json(fs_p, {})
    series = _snapshot_series(directory)
    if not series:
        sys.exit("no dated registry snapshots found")
    prev_t, prev_keys, n = None, set(), 0
    for t, reg in series:
        keys = set(stealth_slots(reg))
        for code in keys - prev_keys:
            k = "p:" + code
            if str(fs.get(k, "before")).startswith("before"):
                fs[k] = (f"{iso(prev_t)} .. {iso(t)}" if prev_t else f"on or before {iso(t)}")
                n += 1
        prev_t, prev_keys = t, keys
    save_json(fs_p, fs)
    print(f"seeded {n} first-seen windows from {len(series)} snapshots "
          f"({iso(series[0][0])} .. {iso(series[-1][0])})")


def _seen_end(v):
    """Latest timestamp a first-seen value allows, for sorting/filtering."""
    stamps = re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", str(v or ""))
    return stamps[-1] if stamps else ""


def cmd_replay(directory, cfg, show_payloads):
    files = sorted(f for f in os.listdir(directory) if f.endswith(".json") and "cdx" not in f)
    prev, fs, total = None, {}, 0
    for f in files:
        with open(os.path.join(directory, f)) as fh:
            try:
                reg = json.load(fh)
                validate(reg)
            except Exception as e:
                print(f"skip {f}: {e}")
                continue
        stamp = re.search(r"(\d{14})", f)
        now = dt.datetime.strptime(stamp.group(1), "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc) \
            if stamp else utcnow()
        if prev is None:
            prev = reg
            print(f"== {f}: baseline, {len(stealth_slots(reg))} stealth slots")
            continue
        ev = diff(prev, reg, now, fs)
        total += len(ev)
        counts = {}
        for e in ev:
            counts[e["type"]] = counts.get(e["type"], 0) + 1
        print(f"== {f}: {len(ev)} events {counts}")
        for e in ev:
            if e["type"] == "stealth_revealed":
                print(f"   🎭 {e['codename']} -> {e['model']['name']} ({e['model']['lab']})")
            elif e["type"] == "stealth_added":
                print(f"   🕵️ {e['codename']}" + (f"  [{e['family']}]" if e.get("family") else ""))
            elif e["type"] == "stealth_removed":
                print(f"   🫥 {e['codename']} removed (no codename model)")
        if show_payloads:
            for p in payloads_for(ev, cfg):
                print(json.dumps(p, ensure_ascii=False)[:1500])
        prev = reg
    print(f"total events: {total}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["once", "loop", "status", "baseline", "replay", "seed", "test-post"])
    ap.add_argument("dir", nargs="?")
    ap.add_argument("--interval", type=int, default=120)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--post", action="store_true", help="status/baseline: post the roster to Discord")
    ap.add_argument("--only", nargs="*", help="status/baseline: only these codenames")
    ap.add_argument("--since", help="status/baseline: only slots first seen after this ISO date")
    ap.add_argument("--days", type=int, help="status/baseline: only slots first seen in the last N days")
    ap.add_argument("--payloads", action="store_true", help="replay: print Discord payloads")
    a = ap.parse_args()
    if a.days and not a.since:
        a.since = iso(utcnow() - dt.timedelta(days=a.days))
    cfg = load_config()
    if a.cmd == "once":
        poll_once(cfg, a.dry_run)
    elif a.cmd == "loop":
        cmd_loop(cfg, a.interval, a.dry_run)
    elif a.cmd in ("status", "baseline"):
        cmd_status(a.post, cfg, a.dry_run, a.only, a.since)
    elif a.cmd in ("replay", "seed"):
        if not a.dir:
            sys.exit(f"{a.cmd} needs a directory of dated registry snapshots")
        if a.cmd == "seed":
            cmd_seed(a.dir)
        else:
            cmd_replay(a.dir, cfg, a.payloads)
    elif a.cmd == "test-post":
        post(cfg, [{"username": cfg["username"], "content": "✅ Design Arena watch is connected.",
                    "allowed_mentions": {"parse": []}}], dry_run=a.dry_run)


if __name__ == "__main__":
    main()
