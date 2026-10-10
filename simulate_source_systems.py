"""
Source system simulator - the OLTP / operational layer.
=======================================================
FULLY SYNTHETIC. A synthetic scenario for a fictional operator ("Voltway
Networks"). No real operator, customer, site or telemetry data is used, copied
or referenced, and nothing here describes any real company.

This script does NOT compute reliability, impact or any other business metric.
It only emits what operational source systems would actually hand over:

    raw/raw_station_inventory.csv     asset register
    raw/raw_charger_inventory.csv     asset register
    raw/raw_connector_inventory.csv   asset register
    raw/raw_customer_accounts.csv     CRM extract (no PII)
    raw/raw_ocpp_status_events.csv    OCPP 1.6 StatusNotification, on change
    raw/raw_charging_attempts.csv     CPMS transaction records, meter readings
    raw/raw_payment_transactions.csv  payment processor extract
    raw/raw_fault_events.csv          charger management system fault log
    raw/raw_alert_events.csv          existing rule-based monitoring alerts

Everything analytical - status intervals, reliability, silent-failure
detection, commercial impact - is derived downstream in build_marts.py.
That separation is the point: the copilot never reads this layer.

WHY THE STATUS EVENTS MATTER
----------------------------
OCPP reports on change, not on a timer. A working session walks
    Available -> Preparing -> Charging -> Available
A charger that cannot deliver walks
    Available -> Preparing -> Available
and never reaches Charging. It never enters Faulted or Unavailable, so nothing
in the status stream reads as a problem and no fault is logged. Status-derived
reporting therefore sees a healthy unit while the session evidence shows the
opposite. That contrast is expressed here in the raw data rather than asserted
later.

GENERATION ORDER
----------------
Impairment windows are decided FIRST, attempts are generated against them
SECOND, status events are rendered from both LAST. Generating them
independently produces a dataset that contradicts itself.

Run:  python simulate_source_systems.py
"""

from __future__ import annotations

import csv
import json
import random
from collections import defaultdict
from datetime import datetime, timedelta, date
from pathlib import Path

SEED = 20260910
random.seed(SEED)

ROOT = Path(__file__).parent
RAW = ROOT / "raw"
RAW.mkdir(parents=True, exist_ok=True)

OPERATOR = "Voltway Networks"
EXTRACTED_AT = "2026-09-05T02:00:00"

TODAY = date(2026, 9, 5)
DAYS = 30
START = TODAY - timedelta(days=DAYS)
D0 = datetime.combine(START, datetime.min.time())
D1 = datetime.combine(TODAY, datetime.min.time())

# ---------------------------------------------------------------- pilot region

CITIES = [
    # Midlands and North of England pilot. London is deliberately out of scope:
    # you validate a new operational process on a meaningful regional estate
    # before taking it to your most visible sites.
    ("Birmingham",     "West Midlands", 52.4862, -1.8904, 22),
    ("Manchester",     "North West",    53.4808, -2.2426, 20),
    ("Leeds",          "Yorkshire",     53.8008, -1.5491, 15),
    ("Liverpool",      "North West",    53.4084, -2.9916, 12),
    ("Sheffield",      "Yorkshire",     53.3811, -1.4701, 12),
    ("Nottingham",     "East Midlands", 52.9548, -1.1581, 11),
    ("Newcastle",      "North East",    54.9783, -1.6178, 10),
    ("Leicester",      "East Midlands", 52.6369, -1.1398, 10),
    ("Derby",          "East Midlands", 52.9225, -1.4746,  8),
    ("Coventry",       "West Midlands", 52.4068, -1.5197,  8),
    ("Stoke-on-Trent", "West Midlands", 53.0027, -2.1794,  7),
    ("York",           "Yorkshire",     53.9600, -1.0873,  6),
    ("Hull",           "Yorkshire",     53.7676, -0.3274,  5),
    ("Wolverhampton",  "West Midlands", 52.5862, -2.1288,  4),
]

TARGET_SITES, TARGET_CHARGERS = 150, 600

SITE_SIZE = {"motorway": (6, 9), "airport": (5, 8), "city_centre": (3, 5),
             "retail_park": (3, 5), "office": (2, 4), "residential": (2, 3)}
SITE_TYPE_WEIGHTS = {"motorway": 15, "retail_park": 25, "city_centre": 22,
                     "office": 16, "residential": 17, "airport": 5}
SITE_DEMAND = {"motorway": (1.10, 1.60), "airport": (1.00, 1.40),
               "city_centre": (0.80, 1.30), "retail_park": (0.50, 1.00),
               "office": (0.30, 0.90), "residential": (0.35, 0.80)}
PARTNER_TYPES = {"motorway": "motorway_services", "retail_park": "retail_partner",
                 "office": "workplace", "residential": "on_street",
                 "city_centre": "retail_partner", "airport": "airport_operator"}

DEMAND_SHAPES = {
    "motorway":    [2, 1, 1, 1, 2, 5, 12, 20, 22, 14, 10, 9, 9, 10, 11, 13, 18, 24, 22, 14, 8, 5, 3, 2],
    "office":      [1, 1, 1, 1, 1, 3, 9, 18, 24, 22, 16, 12, 11, 12, 14, 16, 14, 8, 4, 2, 2, 1, 1, 1],
    "residential": [4, 3, 2, 2, 2, 3, 6, 9, 7, 6, 6, 7, 7, 7, 8, 10, 16, 24, 26, 22, 16, 11, 8, 6],
    "retail_park": [1, 1, 1, 1, 1, 2, 4, 8, 13, 18, 22, 24, 23, 22, 20, 18, 15, 12, 8, 5, 3, 2, 1, 1],
    "city_centre": [3, 2, 2, 1, 2, 4, 8, 14, 18, 19, 18, 19, 20, 19, 18, 18, 19, 20, 19, 16, 12, 8, 6, 4],
    "airport":     [8, 6, 5, 5, 7, 12, 16, 18, 17, 16, 16, 16, 16, 16, 16, 17, 17, 17, 16, 15, 14, 12, 10, 9],
}

CHARGER_MODELS = [("VoltCore R150", 150), ("VoltCore R120", 120), ("VoltCore U350", 350),
                  ("Kempower S75", 75), ("Alpitronic HYC200", 200), ("Tritium PK75", 75)]
FIRMWARES = ["3.2.1", "3.4.0", "4.0.2", "4.1.0"]
SEGMENTS = ["retail_payg", "subscription", "fleet", "roaming"]
TARIFF = {"retail_payg": "BASE_RAPID", "subscription": "SUBSCRIBER_RAPID",
          "fleet": "BASE_RAPID", "roaming": "ROAMING_RAPID"}

FAULT_CODES = ["ConnectorLockFailure", "OverTemperature", "PowerMeterFailure",
               "GroundFailure", "InternalError"]
BASE_PRICE = 0.79

# Baseline attempt failure rates present on any network, independent of the
# planted impairments: handshake failures, declines, drivers giving up.
P_ABORT_SHORT, P_PAY_FAIL, P_ZERO_BASE, P_RETRY = 0.019, 0.015, 0.008, 0.35

# Impairment kinds that leave every emitted status code reading as operational.
INVISIBLE = {"silent", "low_power", "connector_dead", "payment_terminal", "abort_heavy"}


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------- inventory

def build_stations():
    """~150 sites. The first six are a fixed fixture so the demo's hero charger
    always lands at Birmingham Motorway 006 with five same-site peers."""
    geo = {c[0]: c for c in CITIES}
    quota = {c[0]: c[4] for c in CITIES}
    out = []

    def make(city, lt, n_ch):
        _, region, lat, lon, _ = geo[city]
        i = len(out) + 1
        lo, hi = SITE_DEMAND[lt]
        out.append({
            "station_id": "STN-{:03d}".format(i),
            "station_name": "{} {} {:03d}".format(city, lt.replace("_", " ").title(), i),
            "city": city, "region": region, "location_type": lt,
            "partner_location_type": PARTNER_TYPES[lt],
            "latitude": round(lat + random.uniform(-0.11, 0.11), 5),
            "longitude": round(lon + random.uniform(-0.11, 0.11), 5),
            "num_chargers": n_ch,
            "is_24_7": "Y" if lt in ("motorway", "airport", "city_centre") else "N",
            "commissioned_date": (TODAY - timedelta(days=random.randint(200, 1600))).isoformat(),
            "_mult": round(random.uniform(lo, hi), 2),
            "_rel": round(random.uniform(0.965, 0.995), 4),
        })
        quota[city] -= 1

    for city, lt, n in [("Birmingham", "retail_park", 6), ("Manchester", "motorway", 8),
                        ("Leeds", "city_centre", 5), ("Birmingham", "city_centre", 6),
                        ("Sheffield", "retail_park", 5), ("Birmingham", "motorway", 6)]:
        make(city, lt, n)
    out[5]["_mult"], out[5]["_rel"] = 1.55, 0.990        # hero site: busy motorway hub

    types = list(SITE_TYPE_WEIGHTS)
    weights = [SITE_TYPE_WEIGHTS[t] for t in types]
    while len(out) < TARGET_SITES:
        avail = [c for c in quota if quota[c] > 0]
        city = random.choices(avail, weights=[quota[c] for c in avail])[0]
        lt = random.choices(types, weights=weights)[0]
        if lt == "airport" and geo[city][4] < 10:
            lt = "city_centre"
        make(city, lt, random.randint(*SITE_SIZE[lt]))

    total, guard = sum(s["num_chargers"] for s in out), 0
    while total != TARGET_CHARGERS and guard < 20000:
        guard += 1
        st = random.choice(out[6:])
        lo, hi = SITE_SIZE[st["location_type"]]
        if total > TARGET_CHARGERS and st["num_chargers"] > lo:
            st["num_chargers"] -= 1
            total -= 1
        elif total < TARGET_CHARGERS and st["num_chargers"] < hi:
            st["num_chargers"] += 1
            total += 1
    return out


def build_chargers(stations):
    chargers, connectors = [], []
    ci = ni = 0
    for st in stations:
        comm = date.fromisoformat(st["commissioned_date"])
        stagger = max(0, (TODAY - comm).days - 30)
        for _ in range(st["num_chargers"]):
            ci += 1
            model, power = random.choice(CHARGER_MODELS)
            cid = "CHG-{:04d}".format(ci)
            n_conn = 2 if power >= 120 else 1
            chargers.append({
                "charger_id": cid, "station_id": st["station_id"], "model": model,
                "max_power_kw": power, "connector_count": n_conn,
                "installed_date": (comm + timedelta(days=random.choice(
                    [0, 0, 0, random.randint(0, min(400, stagger))]))).isoformat(),
                "firmware_version": random.choice(FIRMWARES),
                "_rel": st["_rel"],
            })
            for k in range(n_conn):
                ni += 1
                connectors.append({
                    "connector_id": "CON-{:05d}".format(ni), "charger_id": cid,
                    "connector_ordinal": k + 1,
                    "connector_type": "CCS" if k == 0 else random.choice(["CHAdeMO", "Type2"]),
                    "max_power_kw": power if k == 0 else min(power, 50),
                })
    return chargers, connectors


def build_customers(stations, n=15000):
    pool = [s["region"] for s in stations for _ in range(s["num_chargers"])]
    out = []
    for i in range(1, n + 1):
        seg = random.choices(SEGMENTS, weights=[58, 22, 14, 6])[0]
        if seg == "fleet":
            k = random.uniform(38, 62)
        elif seg == "subscription":
            k = random.uniform(28, 48)
        elif seg == "roaming":
            k = random.uniform(20, 45)
        else:
            k = random.uniform(18, 42)
        out.append({
            "customer_id": "CUS-{:06d}".format(i), "segment": seg,
            "home_region": random.choice(pool),
            "signup_date": (TODAY - timedelta(days=random.randint(30, 1100))).isoformat(),
            "is_subscriber": "Y" if seg == "subscription" else "N",
            "_avg_kwh": round(k, 1),
        })
    return out


# ---------------------------------------------------------------- impairments

def build_impairments(chargers, connectors):
    """Physical reality, decided before anything else happens.

    35 chargers carry an impairment that emits no fault and triggers no alert.
    That portfolio - not any single unit - is what the copilot has to surface.
    """
    conn_by = defaultdict(list)
    for c in connectors:
        conn_by[c["charger_id"]].append(c)
    W = defaultdict(list)

    def add(cid, s0, e0, kind, meta=None):
        W[cid].append((max(s0, D0), min(e0, D1), kind, meta or {}))

    HERO = "CHG-0034"
    add(HERO, D1 - timedelta(days=3), D1, "silent")
    impaired = {HERO}
    registry = {"silent": [HERO], "connector_dead": [], "payment_terminal": [],
                "low_power": [], "abort_heavy": []}

    pool = [c for c in chargers if c["charger_id"] != HERO]
    random.shuffle(pool)
    dual = [c for c in pool if c["connector_count"] == 2]

    def take(seq):
        for c in seq:
            if c["charger_id"] not in impaired:
                impaired.add(c["charger_id"])
                return c
        return None

    for _ in range(7):                                   # shorter than the hero
        c = take(pool)
        d, off = random.choice([1, 1, 2]), random.randint(4, 22)
        add(c["charger_id"], D1 - timedelta(days=off), D1 - timedelta(days=off - d), "silent")
        registry["silent"].append(c["charger_id"])

    for _ in range(9):
        c = take(dual)
        if not c:
            break
        d = random.randint(2, 6)
        off = random.randint(d, 20)
        add(c["charger_id"], D1 - timedelta(days=off), D1 - timedelta(days=off - d),
            "connector_dead", {"connector_id": conn_by[c["charger_id"]][1]["connector_id"]})
        registry["connector_dead"].append(c["charger_id"])

    for _ in range(7):
        c = take(pool)
        d = random.randint(2, 6)
        off = random.randint(d, 18)
        add(c["charger_id"], D1 - timedelta(days=off), D1 - timedelta(days=off - d),
            "payment_terminal", {"method": "contactless"})
        registry["payment_terminal"].append(c["charger_id"])

    for _ in range(6):
        c = take([x for x in pool if x["max_power_kw"] >= 120])
        if not c:
            break
        d = random.randint(4, 10)
        off = random.randint(d, 20)
        add(c["charger_id"], D1 - timedelta(days=off), D1 - timedelta(days=off - d),
            "low_power", {"factor": 0.06})
        registry["low_power"].append(c["charger_id"])

    for _ in range(5):
        c = take(pool)
        d = random.randint(3, 8)
        off = random.randint(d, 16)
        add(c["charger_id"], D1 - timedelta(days=off), D1 - timedelta(days=off - d),
            "abort_heavy", {"p": random.uniform(0.35, 0.6)})
        registry["abort_heavy"].append(c["charger_id"])

    # ---- VISIBLE failures. These raise faults and fill the alert queue the
    # operations team already works through every morning.
    visible = [c for c in pool if c["charger_id"] not in impaired]
    hard, recov, flap = visible[:3], visible[3:8], visible[8:16]
    for i, c in enumerate(hard):
        add(c["charger_id"], D1 - timedelta(days=1 + i, hours=6 + i * 3), D1, "offline")
    for c in recov:
        o = random.randint(2, 20)
        add(c["charger_id"], D1 - timedelta(days=o, hours=9),
            D1 - timedelta(days=o, hours=random.randint(2, 7)), "offline")
    for c in flap:
        t = D1 - timedelta(days=random.randint(4, 9))
        while t < D1 - timedelta(hours=2):
            d = random.randint(20, 55)
            add(c["charger_id"], t, t + timedelta(minutes=d), "faulted",
                {"code": random.choice(FAULT_CODES)})
            t += timedelta(hours=random.randint(6, 20))

    scripted = impaired | {c["charger_id"] for c in hard + recov + flap}
    for c in chargers:
        if c["charger_id"] in scripted:
            continue
        budget = max(0.0, random.gauss((1 - c["_rel"]) * 720 * 0.42,
                                       (1 - c["_rel"]) * 720 * 0.42 * 0.4)) * 60
        while budget > 40:
            dur = min(budget, random.choice([60, 110, 180, 260, 380]))
            st = D0 + timedelta(minutes=random.randint(0, DAYS * 1440 - int(dur) - 1))
            kind = "offline" if random.random() < 0.35 else "faulted"
            add(c["charger_id"], st, st + timedelta(minutes=dur), kind,
                {"code": random.choice(FAULT_CODES)} if kind == "faulted" else {})
            budget -= dur

    for k in W:
        W[k].sort(key=lambda w: w[0])
    return dict(W), {"hero": HERO, "impaired": sorted(impaired),
                     "by_pattern": {k: sorted(v) for k, v in registry.items()}}


def window_at(W, cid, t):
    for (s, e, kind, meta) in W.get(cid, ()):
        if s <= t < e:
            return kind, meta
    return None, {}


# ---------------------------------------------------------------- attempts

def pick_hour(shape, weekend, lt):
    w = list(shape)
    if weekend:
        if lt == "office":
            w = [max(1, int(x * 0.25)) for x in w]
        elif lt == "retail_park":
            w = [int(x * 1.35) for x in w]
        elif lt == "motorway":
            w = [int(x * 0.9) for x in w]
    return random.choices(range(24), weights=w)[0]


def build_attempts(stations, chargers, connectors, customers, W):
    """CPMS transaction records. Meter readings, not kWh - the transform derives
    energy. A driver only turns up if the app offered the unit, so offline and
    faulted windows produce no transaction at all."""
    by_station = defaultdict(list)
    for c in chargers:
        by_station[c["station_id"]].append(c)
    conns = defaultdict(list)
    for c in connectors:
        conns[c["charger_id"]].append(c)

    rows = []
    meter = defaultdict(lambda: random.randint(1_000_000, 9_000_000))   # Wh lifetime

    def make(ch, cust, start, connector_id):
        kind, meta = window_at(W, ch["charger_id"], start)
        if kind in ("offline", "faulted"):
            return None

        target = min(max(3.0, random.gauss(cust["_avg_kwh"], 8)), ch["max_power_kw"] * 0.85)
        eff = ch["max_power_kw"] * random.uniform(0.45, 0.85)
        method = random.choices(["contactless", "app", "rfid_card", "roaming_token"],
                               weights=[42, 34, 16, 8])[0]
        price = BASE_PRICE * (0.92 if cust["segment"] == "subscription"
                              else 1.08 if cust["segment"] == "roaming" else 1.0)
        kwh = target
        dur = max(6, int(target / eff * 60 + random.uniform(2, 9)))
        stop_reason, delivered = "Local", True

        if kind == "silent":
            delivered, stop_reason = False, "EVDisconnected"
        elif kind == "connector_dead" and connector_id == meta.get("connector_id"):
            delivered, stop_reason = False, "EVDisconnected"
        elif kind == "payment_terminal" and method == meta.get("method"):
            delivered, stop_reason = False, "DeAuthorized"
        elif kind == "abort_heavy" and random.random() < meta.get("p", 0.5):
            delivered, stop_reason = False, "PowerLoss"
        elif kind == "low_power":
            eff = max(7.0, ch["max_power_kw"] * meta.get("factor", 0.06))
            dur = max(15, int(target / eff * 60))
            if dur > 95:
                dur = random.randint(55, 95)
                kwh = round(eff * dur / 60, 2)
        else:
            r = random.random()
            if r < P_ABORT_SHORT:
                delivered, stop_reason = False, "PowerLoss"
            elif r < P_ABORT_SHORT + P_PAY_FAIL:
                delivered, stop_reason = False, "DeAuthorized"
            elif r < P_ABORT_SHORT + P_PAY_FAIL + P_ZERO_BASE:
                delivered, stop_reason = False, "EVDisconnected"

        if not delivered:
            if stop_reason == "PowerLoss":
                kwh, dur = round(random.uniform(0.0, 0.6), 2), random.randint(1, 3)
            else:
                kwh, dur = 0.0, random.randint(2, 9)

        m0 = meter[ch["charger_id"]]
        m1 = m0 + int(round(kwh * 1000))
        meter[ch["charger_id"]] = m1
        return {
            "transaction_id": None, "charger_id": ch["charger_id"],
            "connector_id": connector_id, "customer_id": cust["customer_id"],
            "start_ts": iso(start), "stop_ts": iso(start + timedelta(minutes=dur)),
            "meter_start_wh": m0, "meter_stop_wh": m1, "stop_reason": stop_reason,
            "auth_method": method, "tariff_code": TARIFF[cust["segment"]],
            "unit_price_gbp_per_kwh": round(price, 4),
            "_start": start, "_end": start + timedelta(minutes=dur),
            "_kwh": kwh, "_ok": delivered, "_method": method,
        }

    for st in stations:
        shape = DEMAND_SHAPES[st["location_type"]]
        chs = by_station[st["station_id"]]
        for d in range(DAYS):
            day = START + timedelta(days=d)
            wknd = day.weekday() >= 5
            base = 9.0 * st["_mult"]
            if wknd:
                base *= 1.15 if st["location_type"] in ("retail_park", "city_centre") else 0.82
            for _ in range(max(0, int(random.gauss(base * len(chs), base * 0.9)))):
                ch = random.choice(chs)
                cust = random.choice(customers)
                hr = pick_hour(shape, wknd, st["location_type"])
                start = (datetime.combine(day, datetime.min.time())
                         + timedelta(hours=hr, minutes=random.randint(0, 59)))
                conn = random.choice(conns[ch["charger_id"]])["connector_id"]
                row = make(ch, cust, start, conn)
                if row is None:
                    continue
                rows.append(row)
                tries = 0
                while not row["_ok"] and tries < 2 and random.random() < P_RETRY:
                    tries += 1
                    again = make(ch, cust, row["_end"] + timedelta(
                        minutes=random.randint(2, 18)), conn)
                    if again is None:
                        break
                    rows.append(again)
                    row = again

    rows.sort(key=lambda r: r["_start"])
    for i, r in enumerate(rows, start=1):
        r["transaction_id"] = "TXN-{:08d}".format(i)
    return rows


def build_payments(attempts):
    out = []
    for i, a in enumerate(attempts, start=1):
        if a["_ok"]:
            status, code = "settled", ""
        elif a["stop_reason"] == "DeAuthorized":
            status = "declined"
            code = random.choice(["card_declined", "terminal_timeout", "insufficient_funds"])
        else:
            status, code = "not_captured", "no_energy_delivered"
        out.append({
            "payment_id": "PAY-{:08d}".format(i), "transaction_id": a["transaction_id"],
            "amount_gbp": round(a["_kwh"] * a["unit_price_gbp_per_kwh"], 2) if a["_ok"] else 0.0,
            "currency": "GBP", "status": status, "method": a["_method"],
            "attempted_ts": a["stop_ts"],
            "settled_ts": iso(a["_end"] + timedelta(minutes=2)) if a["_ok"] else "",
            "decline_code": code,
        })
    return out


# ---------------------------------------------------------------- status events

def build_status_events(chargers, attempts, W):
    """OCPP 1.6 StatusNotification, emitted on change.

    A working session walks Available -> Preparing -> Charging -> Available.
    A charger that cannot deliver walks Available -> Preparing -> Available and
    never reaches Charging. Status-based reporting counts every one of those
    states as operational.
    """
    by_charger = defaultdict(list)
    for a in attempts:
        by_charger[a["charger_id"]].append(a)

    ev = []

    def emit(cid, conn, t, status, code=""):
        ev.append({"event_id": None, "charger_id": cid, "connector_id": conn,
                   "event_ts": iso(t), "status": status, "error_code": code,
                   "vendor_id": "voltway.ocpp16", "source_system": "cpms_ocpp"})

    for ch in chargers:
        cid = ch["charger_id"]
        emit(cid, "", D0, "Available")
        for (s, e, kind, meta) in W.get(cid, ()):
            if kind == "offline":
                emit(cid, "", s, "Unavailable", "CommunicationError")
                if e < D1:
                    emit(cid, "", e, "Available")
            elif kind == "faulted":
                emit(cid, "", s, "Faulted", meta.get("code", "InternalError"))
                if e < D1:
                    emit(cid, "", e, "Available")
        for a in by_charger.get(cid, ()):
            conn = a["connector_id"]
            emit(cid, conn, a["_start"], "Preparing")
            if a["_ok"] or a["_kwh"] > 0:
                emit(cid, conn, a["_start"] + timedelta(seconds=45), "Charging")
            emit(cid, conn, a["_end"], "Available")

    ev.sort(key=lambda e: (e["event_ts"], e["charger_id"]))
    for i, e in enumerate(ev, start=1):
        e["event_id"] = "EVT-{:08d}".format(i)
    return ev


def build_fault_log(W, chargers):
    """Charger management system fault log. Only the two visible impairment
    kinds appear here - that is what makes the other five hard to find."""
    st = {c["charger_id"]: c["station_id"] for c in chargers}
    out = []
    for cid, wins in sorted(W.items()):
        for (s, e, kind, meta) in wins:
            if kind not in ("offline", "faulted"):
                continue
            out.append({
                "fault_id": None, "charger_id": cid, "station_id": st[cid],
                "opened_ts": iso(s), "closed_ts": "" if e >= D1 else iso(e),
                "fault_code": "CommunicationError" if kind == "offline"
                              else meta.get("code", "InternalError"),
                "severity_raw": "critical" if kind == "offline" else "major",
                "source_system": "cms_fault_log",
            })
    out.sort(key=lambda f: (f["opened_ts"], f["charger_id"]))
    for i, f in enumerate(out, start=1):
        f["fault_id"] = "FLT-{:06d}".format(i)
    return out


def build_alert_log(faults):
    """The operator's existing rule-based monitoring. It watches the fault log,
    so it can only ever see what the fault log contains."""
    horizon = D1 - timedelta(days=7)
    recent = [f for f in faults if datetime.strptime(f["opened_ts"], "%Y-%m-%dT%H:%M:%S") >= horizon]
    dur = {}
    for f in recent:
        o = datetime.strptime(f["opened_ts"], "%Y-%m-%dT%H:%M:%S")
        c = datetime.strptime(f["closed_ts"], "%Y-%m-%dT%H:%M:%S") if f["closed_ts"] else D1
        dur[f["fault_id"]] = int((c - o).total_seconds() // 60)
    counts = defaultdict(int)
    for f in recent:
        counts[f["charger_id"]] += 1

    out, seen = [], set()
    for f in recent:
        rule = msg = None
        if f["fault_code"] == "CommunicationError" and dur[f["fault_id"]] >= 60:
            rule = "R-A-OFFLINE-60"
            msg = "Charger unreachable for {} minutes".format(dur[f["fault_id"]])
        elif counts[f["charger_id"]] >= 3 and f["charger_id"] not in seen:
            rule = "R-B-REPEAT-3"
            msg = "{} faults in the last 7 days".format(counts[f["charger_id"]])
            seen.add(f["charger_id"])
        if not rule:
            continue
        out.append({"alert_id": None, "charger_id": f["charger_id"],
                    "station_id": f["station_id"], "created_ts": f["opened_ts"],
                    "rule_id": rule, "severity": "high", "message": msg,
                    "status": "open", "source_fault_id": f["fault_id"],
                    "source_system": "ops_monitoring"})
    out.sort(key=lambda a: a["created_ts"])
    for i, a in enumerate(out, start=1):
        a["alert_id"] = "ALR-{:06d}".format(i)
    return out


# ---------------------------------------------------------------- io

def write(name, rows, cols):
    path = RAW / name
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print("  {:36s} {:>9,} rows".format(name, len(rows)))


def main():
    print("\nSimulating operational source systems for {} (seed={})\n".format(OPERATOR, SEED))

    stations = build_stations()
    chargers, connectors = build_chargers(stations)
    customers = build_customers(stations)
    W, patterns = build_impairments(chargers, connectors)
    attempts = build_attempts(stations, chargers, connectors, customers, W)
    payments = build_payments(attempts)
    events = build_status_events(chargers, attempts, W)
    faults = build_fault_log(W, chargers)
    alerts = build_alert_log(faults)

    for s in stations:
        s["extracted_at"] = EXTRACTED_AT
    for c in chargers:
        c["extracted_at"] = EXTRACTED_AT
    for c in connectors:
        c["extracted_at"] = EXTRACTED_AT
    for c in customers:
        c["extracted_at"] = EXTRACTED_AT

    write("raw_station_inventory.csv", stations,
          ["station_id", "station_name", "city", "region", "location_type",
           "partner_location_type", "latitude", "longitude", "num_chargers",
           "is_24_7", "commissioned_date", "extracted_at"])
    write("raw_charger_inventory.csv", chargers,
          ["charger_id", "station_id", "model", "max_power_kw", "connector_count",
           "installed_date", "firmware_version", "extracted_at"])
    write("raw_connector_inventory.csv", connectors,
          ["connector_id", "charger_id", "connector_ordinal", "connector_type",
           "max_power_kw", "extracted_at"])
    write("raw_customer_accounts.csv", customers,
          ["customer_id", "segment", "home_region", "signup_date", "is_subscriber",
           "extracted_at"])
    write("raw_ocpp_status_events.csv", events,
          ["event_id", "charger_id", "connector_id", "event_ts", "status",
           "error_code", "vendor_id", "source_system"])
    write("raw_charging_attempts.csv", attempts,
          ["transaction_id", "charger_id", "connector_id", "customer_id",
           "start_ts", "stop_ts", "meter_start_wh", "meter_stop_wh", "stop_reason",
           "auth_method", "tariff_code", "unit_price_gbp_per_kwh"])
    write("raw_payment_transactions.csv", payments,
          ["payment_id", "transaction_id", "amount_gbp", "currency", "status",
           "method", "attempted_ts", "settled_ts", "decline_code"])
    write("raw_fault_events.csv", faults,
          ["fault_id", "charger_id", "station_id", "opened_ts", "closed_ts",
           "fault_code", "severity_raw", "source_system"])
    write("raw_alert_events.csv", alerts,
          ["alert_id", "charger_id", "station_id", "created_ts", "rule_id",
           "severity", "message", "status", "source_fault_id", "source_system"])

    manifest = {
        "operator": OPERATOR, "seed": SEED, "extracted_at": EXTRACTED_AT,
        "disclaimer": "Fully synthetic scenario. No real operator, site, customer "
                      "or telemetry data is used, copied or referenced.",
        "pilot": {
            "name": "Midlands and North of England pilot",
            "regions": sorted({s["region"] for s in stations}),
            "cities": sorted({s["city"] for s in stations}),
            "excludes": "London",
            "note": "Representative regional pilot, not a full national estate.",
        },
        "window": {"start": START.isoformat(), "end": TODAY.isoformat(), "days": DAYS},
        "counts": {"stations": len(stations), "chargers": len(chargers),
                   "connectors": len(connectors), "customers": len(customers),
                   "status_events": len(events), "charging_attempts": len(attempts),
                   "payments": len(payments), "faults": len(faults), "alerts": len(alerts)},
        "ground_truth": {
            "note": "Held for validation only. The transformation layer must DETECT "
                    "these from the data, never read them from here.",
            "hero": patterns["hero"],
            "impaired_chargers": patterns["impaired"],
            "by_pattern": patterns["by_pattern"],
            "invisible_to_status_reporting": sorted(INVISIBLE),
            "visible_to_status_reporting": ["offline", "faulted"],
        },
    }
    (RAW / "_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print("  {:36s} {:>9}".format("_manifest.json", "1"))
    print("\n  {} chargers · {} sites · {:,} attempts · {:,} status events".format(
        len(chargers), len(stations), len(attempts), len(events)))
    print("  {} impaired chargers planted, {} faults logged, {} alerts raised\n".format(
        len(patterns["impaired"]), len(faults), len(alerts)))


if __name__ == "__main__":
    main()
