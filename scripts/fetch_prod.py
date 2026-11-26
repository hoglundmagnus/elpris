#!/usr/bin/env python3
"""Hämtar elproduktion och kärnkraftsstatus och skriver data/prod.json.

Körs av GitHub Actions var 15:e minut (källorna tillåter inte anrop direkt
från webbläsaren). Bara standardbiblioteket används.

Källor:
  - Svenska kraftnät, Kontrollrummet: produktion per kraftslag idag (per kvart)
  - Energy-Charts (Fraunhofer ISE, CC BY 4.0): historik per kraftslag, 1 år
  - Nord Pool UMM: driftmeddelanden för kärnkraftsreaktorerna
"""
import json, os, sys, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Stockholm")
UA = {"User-Agent": "Mozilla/5.0 (elpris-app; GitHub Actions)", "Accept": "application/json"}
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "prod.json")

REACTORS = [  # EIC-kod, id, namn, anläggning, ägare, nominell effekt (MW)
    ("46WPU0000000015W", "F1", "Forsmark 1", "Forsmark", "Vattenfall", 984),
    ("46WPU0000000016U", "F2", "Forsmark 2", "Forsmark", "Vattenfall", 1121),
    ("46WPU0000000017S", "F3", "Forsmark 3", "Forsmark", "Vattenfall", 1172),
    ("46WPU00000000121", "R3", "Ringhals 3", "Ringhals", "Vattenfall", 1074),
    ("46WPU0000000014Y", "R4", "Ringhals 4", "Ringhals", "Vattenfall", 1130),
    ("46WPU0000000061P", "O3", "Oskarshamn 3", "Oskarshamn", "OKG", 1400),
]


def get(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    return datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc)


# ---------- Svenska kraftnät: idag per kvart ----------
SVK_IDS = {"2": "nuclear", "3": "hydro", "5": "wind", "6": "thermal", "1": "total", "7": "load", "8": "net"}


def svk_today(now):
    day = now.astimezone(TZ).strftime("%Y-%m-%d")
    j = get(f"https://www.svk.se/services/controlroom/v2/production?date={day}&countryCode=SE")
    out, times = {}, None
    for s in j["Data"]:
        k = SVK_IDS.get(str(s["id"]))
        if not k:
            continue
        pts = sorted(s["data"], key=lambda p: p["x"])
        if times is None or len(pts) > len(times):
            times = [p["x"] for p in pts]
        out[k] = {p["x"]: p["y"] for p in pts}
    if not times or "nuclear" not in out:
        raise ValueError("SVK: oväntat format")
    res = {"src": "svk", "t": times}
    for k, m in out.items():
        res[k] = [m.get(t) for t in times]
    res["solar"] = [None] * len(times)
    return res


# ---------- Energy-Charts: historik ----------
EC_MAP = {"Nuclear": "nuclear", "Hydro water reservoir": "hydro", "Hydro Run-of-River": "hydro",
          "Wind onshore": "wind", "Wind offshore": "wind", "Solar": "solar",
          "Others": "thermal", "Fossil gas": "thermal", "Biomass": "thermal", "Fossil oil": "thermal",
          "Fossil hard coal": "thermal", "Waste": "thermal", "Load": "load"}
TYPES = ["nuclear", "hydro", "wind", "thermal", "solar"]


def energy_charts(now):
    start = (now - timedelta(days=372)).strftime("%Y-%m-%d")
    end = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    j = get(f"https://api.energy-charts.info/public_power?country=se&start={start}&end={end}", timeout=120)
    ts = j["unix_seconds"]
    cols = {}
    for p in j["production_types"]:
        k = EC_MAP.get(p["name"])
        if not k:
            continue
        col = cols.setdefault(k, [None] * len(ts))
        for i, v in enumerate(p["data"]):
            if v is not None:
                col[i] = (col[i] or 0) + v
    return ts, cols


def daily(ts, cols):
    days = {}
    for i, t in enumerate(ts):
        d = datetime.fromtimestamp(t, timezone.utc).astimezone(TZ).strftime("%Y-%m-%d")
        acc = days.setdefault(d, {k: [0.0, 0] for k in TYPES + ["load"]})
        for k in TYPES + ["load"]:
            v = cols.get(k, [None] * len(ts))[i]
            if v is not None:
                acc[k][0] += v
                acc[k][1] += 1
    dates = sorted(days)
    hist = {"d": dates}
    for k in TYPES + ["load"]:
        hist[k] = [round(days[d][k][0] / days[d][k][1]) if days[d][k][1] else None for d in dates]
    return hist


def ec_today(ts, cols, now):
    day = now.astimezone(TZ).strftime("%Y-%m-%d")
    idx = [i for i, t in enumerate(ts) if datetime.fromtimestamp(t, timezone.utc).astimezone(TZ).strftime("%Y-%m-%d") == day]
    idx = [i for i in idx if cols.get("nuclear", [None] * len(ts))[i] is not None]
    if not idx:
        return None
    res = {"src": "energy-charts", "t": [ts[i] * 1000 for i in idx]}
    for k in TYPES + ["load"]:
        res[k] = [None if cols.get(k) is None or cols[k][i] is None else round(cols[k][i]) for i in idx]
    res["total"] = [sum(res[k][n] or 0 for k in TYPES) for n in range(len(idx))]
    res["net"] = [None if res["load"][n] is None else res["total"][n] - res["load"][n] for n in range(len(idx))]
    return res


def installed():
    """Installerad effekt per kraftslag (MW), senaste året som finns."""
    j = get("https://api.energy-charts.info/installed_power?country=se&time_step=yearly&installation_decommission=false")
    years = j["time"]
    out = {}
    for p in j["production_types"]:
        k = EC_MAP.get(p["name"]) or ("solar" if p["name"].startswith("Solar") else None)
        if not k or k == "load":
            continue
        vals = [v for v in p["data"] if v is not None]
        if vals:
            out[k] = out.get(k, 0) + round(vals[-1] * 1000)  # GW -> MW
    out["year"] = years[-1]
    return out


# ---------- Nord Pool UMM: kärnkraft ----------
def reactors(now):
    j = get("https://ummapi.nordpoolgroup.com/messages?limit=2000&fuelTypes=14&messageTypes=1&areas=10Y1001A1001A46L", timeout=120)
    by_eic = {r[0]: {"id": r[1], "name": r[2], "site": r[3], "owner": r[4], "cap": r[5], "events": []} for r in REACTORS}
    latest = {}
    for m in j["items"]:
        if m.get("isOutdated") or m.get("eventStatus") != 1:
            continue
        mid = m.get("messageId")
        if mid in latest and latest[mid]["version"] >= m["version"]:
            continue
        latest[mid] = m
    seen = set()
    for m in latest.values():
        ent = "European Network" in (m.get("publisherName") or "")
        for u in (m.get("productionUnits") or []) + (m.get("generationUnits") or []):
            r = by_eic.get(u.get("eic"))
            if not r:
                continue
            if not ent and u.get("installedCapacity"):
                r.setdefault("_caps", []).append((m["publicationDate"], u["installedCapacity"]))
            for p in u.get("timePeriods") or []:
                a, b = parse_ts(p["eventStart"]), parse_ts(p["eventStop"])
                if b <= now or a > now + timedelta(days=120):
                    continue
                key = (r["id"], p["eventStart"][:16], p["eventStop"][:16], p["availableCapacity"])
                if key in seen:
                    continue
                seen.add(key)
                r["events"].append({
                    "from": iso(a), "to": iso(b),
                    "avail": p["availableCapacity"], "unavail": p["unavailableCapacity"],
                    "planned": m.get("unavailabilityType") == 2,
                    "reason": (m.get("unavailabilityReason") or "").strip()[:240],
                    "pub": m.get("publicationDate", "")[:19] + "Z", "ent": ent,
                })
    out = []
    for r in by_eic.values():
        caps = sorted(r.pop("_caps", []))
        if caps:
            r["cap"] = caps[-1][1]
        ev = r["events"]
        # ENTSO-E speglar ofta ägarens meddelanden; använd dem bara om ägaren saknas
        own = [e for e in ev if not e["ent"]]
        ev = own or ev
        ev.sort(key=lambda e: e["from"])
        active = [e for e in ev if parse_ts(e["from"]) <= now < parse_ts(e["to"])]
        upcoming = [e for e in ev if parse_ts(e["from"]) > now]
        avail = min([e["avail"] for e in active], default=r["cap"])
        avail = max(0, min(avail, r["cap"]))
        cur = min(active, key=lambda e: e["avail"]) if active else None
        r["avail"] = avail
        r["status"] = "off" if avail == 0 else ("limited" if avail < r["cap"] * 0.97 else "on")
        r["now"] = {k: cur[k] for k in ("from", "to", "planned", "reason")} if cur else None
        nxt = []
        for e in upcoming:
            if e["avail"] >= r["cap"] * 0.97:
                continue
            if any(n["from"] == e["from"] and n["to"] == e["to"] for n in nxt):
                continue
            nxt.append({k: e[k] for k in ("from", "to", "avail", "planned", "reason")})
        r["next"] = nxt[:3]
        del r["events"]
        out.append(r)
    return out


def main():
    now = datetime.now(timezone.utc)
    old = {}
    try:
        with open(OUT) as f:
            old = json.load(f)
    except Exception:
        pass
    data = {"updated": iso(now)}
    errors = []

    ts = cols = None
    try:
        ts, cols = energy_charts(now)
        data["hist"] = daily(ts, cols)
    except Exception as e:
        errors.append(f"energy-charts: {e}")
        if "hist" in old:
            data["hist"] = old["hist"]

    try:
        data["today"] = svk_today(now)
    except Exception as e:
        errors.append(f"svk: {e}")
        t = ec_today(ts, cols, now) if ts else None
        if t:
            data["today"] = t
        elif "today" in old:
            data["today"] = old["today"]

    try:
        data["reactors"] = reactors(now)
    except Exception as e:
        errors.append(f"umm: {e}")
        if "reactors" in old:
            data["reactors"] = old["reactors"]

    try:
        data["installed"] = installed()
    except Exception as e:
        errors.append(f"installerad effekt: {e}")
        if "installed" in old:
            data["installed"] = old["installed"]
    # Kärnkraften: summan av reaktorernas nominella effekt är exaktare
    if data.get("installed") and data.get("reactors"):
        data["installed"]["nuclear"] = sum(r["cap"] for r in data["reactors"])
        data["installed"]["nuclear_avail"] = sum(r["avail"] for r in data["reactors"])

    data["errors"] = errors
    if not any(k in data for k in ("today", "hist", "reactors")):
        print("Ingen data alls:", errors, file=sys.stderr)
        sys.exit(1)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print("ok", data["updated"], "fel:", errors,
          "idag:", data.get("today", {}).get("src"), len(data.get("today", {}).get("t", [])),
          "historik:", len(data.get("hist", {}).get("d", [])), "reaktorer:", len(data.get("reactors", [])))


if __name__ == "__main__":
    main()
