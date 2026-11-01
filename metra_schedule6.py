"""
Chicago Metra GTFS Train Schedule Viewer  (Static + Realtime)
==============================================================
Usage:
    python metra_schedule.py --gtfs schedule.zip
    python metra_schedule.py --gtfs schedule.zip --token YOUR_API_KEY

Flow:
  0. Choose Static or Realtime mode
  1. Select a Metra line
  2. View stations on that line (in order)
  3. Pick departure & destination station
  4. View all trains for the day  (with realtime status if enabled)
  5. Select a train → ladder view  (with live positions / delays if enabled)

Realtime feeds: https://gtfspublic.metrarr.com  (refreshed every 30 s)
Get an API key : https://metra.com/developers
"""

import os, sys, re, zipfile, argparse, time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

import pandas as pd
import requests

# ── optional protobuf ────────────────────────────────────────────────────────
try:
    from google.transit import gtfs_realtime_pb2
    PROTO_OK = True
except ImportError:
    PROTO_OK = False

CHICAGO_TZ   = ZoneInfo("America/Chicago")
RT_BASE_URL  = "https://gtfspublic.metrarr.com/gtfs/public"
RT_CACHE_TTL = 30   # seconds — Metra refreshes every 30 s


# ─── Timezone helpers ─────────────────────────────────────────────────────────

def chicago_now() -> datetime:
    """Current naive datetime in Chicago local time."""
    return datetime.now(CHICAGO_TZ).replace(tzinfo=None)

def chicago_today() -> date:
    """Today's date in Chicago."""
    return datetime.now(CHICAGO_TZ).date()

def tz_label() -> str:
    return datetime.now(CHICAGO_TZ).strftime("%Z")


# ─── ANSI colour helpers ──────────────────────────────────────────────────────

RESET   = "\033[0m"
BOLD    = "\033[1m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
CYAN    = "\033[96m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
DIM     = "\033[2m"
WHITE   = "\033[97m"

def c(text, *codes):
    return "".join(codes) + str(text) + RESET

def clear():
    os.system("cls" if os.name == "nt" else "clear")

def header(title: str, subtitle: str = ""):
    width = 66
    print()
    print(c("═" * width, CYAN, BOLD))
    print(c(f"  🚆  {title}", CYAN, BOLD))
    if subtitle:
        print(c(f"       {subtitle}", DIM))
    print(c("═" * width, CYAN, BOLD))
    print()


# ─── GTFS static loader ───────────────────────────────────────────────────────

def load_gtfs(path: str) -> dict:
    needed = ["routes.txt", "trips.txt", "stop_times.txt", "stops.txt", "calendar.txt"]
    frames = {}
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            for fname in needed:
                match = next((n for n in names if n.endswith(fname)), None)
                if match is None:
                    raise FileNotFoundError(f"{fname} not found in zip")
                with zf.open(match) as f:
                    frames[fname] = pd.read_csv(f)
    elif os.path.isdir(path):
        for fname in needed:
            fpath = os.path.join(path, fname)
            if not os.path.exists(fpath):
                raise FileNotFoundError(f"{fpath} not found")
            frames[fname] = pd.read_csv(fpath)
    else:
        raise ValueError(f"Not a zip or directory: {path}")

    for key, df in frames.items():
        df.columns = df.columns.str.strip()
        for col in df.select_dtypes(include=["object", "str"]).columns:
            df[col] = df[col].str.strip()
        frames[key] = df
    return frames


# ─── GTFS-Realtime client ─────────────────────────────────────────────────────

class RealtimeClient:
    """Fetches and caches the three Metra GTFS-RT protobuf feeds."""

    def __init__(self, token: str):
        self.token   = token
        self.headers = {"Authorization": f"Bearer {token}"}
        self._cache: dict = {}

    def _fetch(self, endpoint: str):
        now = time.time()
        if endpoint in self._cache:
            ts, msg = self._cache[endpoint]
            if now - ts < RT_CACHE_TTL:
                return msg
        url = f"{RT_BASE_URL}/{endpoint}"
        try:
            resp = requests.get(url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(resp.content)
            self._cache[endpoint] = (now, feed)
            return feed
        except Exception as e:
            print(c(f"\n  ⚠  Realtime fetch error ({endpoint}): {e}", YELLOW))
            return None

    # ── Public helpers ────────────────────────────────────────────────────

    def get_alerts(self) -> list:
        feed = self._fetch("alerts")
        if not feed:
            return []
        result = []
        for entity in feed.entity:
            if not entity.HasField("alert"):
                continue
            a = entity.alert
            route_ids = list({inf.route_id for inf in a.informed_entity if inf.route_id})
            trip_ids  = list({inf.trip.trip_id for inf in a.informed_entity
                              if inf.HasField("trip") and inf.trip.trip_id})
            header_txt = (a.header_text.translation[0].text
                          if a.header_text.translation else "")
            desc_txt   = (a.description_text.translation[0].text
                          if a.description_text.translation else "")
            result.append({
                "route_ids": route_ids,
                "trip_ids":  trip_ids,
                "header":    header_txt,
                "desc":      desc_txt,
            })
        return result

    def get_positions(self) -> dict:
        """Returns {trip_id: {lat, lon, stop_id, status, timestamp}}"""
        feed = self._fetch("positions")
        if not feed:
            return {}
        out = {}
        for entity in feed.entity:
            if not entity.HasField("vehicle"):
                continue
            v = entity.vehicle
            trip_id = v.trip.trip_id if v.HasField("trip") else ""
            if not trip_id:
                continue
            out[trip_id] = {
                "lat":       v.position.latitude  if v.HasField("position") else None,
                "lon":       v.position.longitude if v.HasField("position") else None,
                "stop_id":   v.stop_id,
                "status":    v.current_status,  # 0=INCOMING_AT, 1=STOPPED_AT, 2=IN_TRANSIT_TO
                "timestamp": v.timestamp,
            }
        return out

    def get_trip_updates(self) -> dict:
        """Returns {trip_id: {schedule_rel, stop_updates: {stop_id: {...}}}}"""
        feed = self._fetch("tripupdates")
        if not feed:
            return {}
        out = {}
        for entity in feed.entity:
            if not entity.HasField("trip_update"):
                continue
            tu      = entity.trip_update
            trip_id = tu.trip.trip_id
            srel    = tu.trip.schedule_relationship  # 0=SCHEDULED, 3=CANCELED, 5=ADDED
            stu_map = {}
            for stu in tu.stop_time_update:
                stu_map[stu.stop_id] = {
                    "arr_delay":  stu.arrival.delay   if stu.HasField("arrival")   else 0,
                    "dep_delay":  stu.departure.delay if stu.HasField("departure") else 0,
                    "arr_ts":     stu.arrival.time    if stu.HasField("arrival")   else None,
                    "dep_ts":     stu.departure.time  if stu.HasField("departure") else None,
                    "skipped":    (stu.schedule_relationship == 1),
                }
            out[trip_id] = {"schedule_rel": srel, "stop_updates": stu_map}
        return out


# ─── Schedule helpers ─────────────────────────────────────────────────────────

def parse_time(t: str):
    """Parse HH:MM:SS; hours ≥ 24 are supported for after-midnight trips."""
    try:
        h, m, s = (int(x) for x in t.strip().split(":"))
        overflow = h // 24
        h = h % 24
        base = datetime.combine(
            chicago_today(),
            datetime.strptime(f"{h:02d}:{m:02d}:{s:02d}", "%H:%M:%S").time()
        )
        return base, overflow
    except Exception:
        return None, 0

def fmt_time(t: str) -> str:
    dt, _ = parse_time(t)
    return dt.strftime("%I:%M %p").lstrip("0") if dt else (t or "—")

def fmt_unix(ts) -> str:
    try:
        dt = datetime.fromtimestamp(int(ts), tz=CHICAGO_TZ).replace(tzinfo=None)
        return dt.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return "—"

def unix_to_dt(ts) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(ts), tz=CHICAGO_TZ).replace(tzinfo=None)
    except Exception:
        return None

def delay_badge(seconds: int) -> str:
    if seconds == 0:
        return c("on time", GREEN)
    m = abs(seconds) // 60
    return c(f"+{m} min late", RED) if seconds > 0 else c(f"{m} min early", CYAN)

def extract_train_no(tid: str) -> str:
    m = re.search(r'_([A-Z]+\d+)_', str(tid))
    return m.group(1) if m else tid

def direction_label(d) -> str:
    try:
        return "Inbound" if int(d) == 1 else "Outbound"
    except Exception:
        return "—"

def get_active_service_ids(calendar: pd.DataFrame) -> list:
    today     = chicago_today()
    weekday   = today.strftime("%A").lower()
    today_int = int(today.strftime("%Y%m%d"))
    mask = (
        (calendar[weekday].astype(int) == 1) &
        (calendar["start_date"].astype(int) <= today_int) &
        (calendar["end_date"].astype(int) >= today_int)
    )
    return calendar[mask]["service_id"].tolist()

def prompt_choice(prompt: str, options: list, display_fn=None) -> int:
    for i, opt in enumerate(options, 1):
        label = display_fn(opt) if display_fn else str(opt)
        print(f"  {c(i, YELLOW, BOLD)}. {label}")
    print()
    while True:
        try:
            raw = input(c(f"  {prompt} ▶  ", CYAN))
            idx = int(raw.strip()) - 1
            if 0 <= idx < len(options):
                return idx
            print(c(f"  Please enter 1–{len(options)}", RED))
        except (ValueError, KeyboardInterrupt):
            print(c("  Invalid input. Try again.", RED))


# ─── Realtime stop-level status ───────────────────────────────────────────────

def stop_rt_status(
    trip_id: str, stop_id: str,
    rt: Optional[RealtimeClient],
    positions: dict, trip_updates: dict,
    sched_arr: str
) -> tuple:
    """
    Returns (time_display_str, status_str).
    Falls back gracefully to static schedule when realtime is missing.
    """
    now = chicago_now()

    # ── Static-only mode ─────────────────────────────────────────────────
    if rt is None:
        arr_dt, _ = parse_time(sched_arr)
        t = fmt_time(sched_arr)
        if arr_dt and arr_dt < now:
            return t, c("✔ Departed", RED)
        if arr_dt:
            mins = int((arr_dt - now).total_seconds() / 60)
            if mins <= 5:
                return t, c(f"⬤ Arriving in {mins} min", YELLOW, BOLD)
            return t, c(f"  Arriving {t}", GREEN)
        return t, c("  Scheduled", DIM)

    # ── Cancelled trip ────────────────────────────────────────────────────
    tu   = trip_updates.get(trip_id, {})
    srel = tu.get("schedule_rel", 0)
    if srel == 3:
        return fmt_time(sched_arr), c("🚫 Cancelled", RED, BOLD)

    # ── Stop-level update available ───────────────────────────────────────
    stu = tu.get("stop_updates", {}).get(stop_id)
    if stu:
        if stu.get("skipped"):
            return fmt_time(sched_arr), c("⛔ Skipped", RED)

        arr_ts  = stu.get("arr_ts")
        arr_del = stu.get("arr_delay", 0)

        if arr_ts:
            arr_dt       = unix_to_dt(arr_ts)
            time_display = fmt_unix(arr_ts)
        else:
            arr_dt, _ = parse_time(sched_arr)
            if arr_dt and arr_del:
                arr_dt = arr_dt + timedelta(seconds=arr_del)
            time_display = arr_dt.strftime("%I:%M %p").lstrip("0") if arr_dt else fmt_time(sched_arr)

        db = f"  ({delay_badge(arr_del)})" if arr_del else ""
        if arr_dt and arr_dt < now:
            return time_display, c("✔ Departed", RED) + db
        if arr_dt:
            mins = int((arr_dt - now).total_seconds() / 60)
            if mins <= 5:
                return time_display, c(f"⬤ Arriving in {mins} min", YELLOW, BOLD) + db
            return time_display, c(f"  Arriving {time_display}", GREEN) + db
        return time_display, c("  Scheduled", DIM)

    # ── No stop-level update → use vehicle position + static time ─────────
    pos = positions.get(trip_id)
    arr_dt, _ = parse_time(sched_arr)
    t = fmt_time(sched_arr)

    if arr_dt and arr_dt < now:
        # Trip has started (GPS present or time-based) → assume departed
        return t, c("✔ Departed", RED)

    if arr_dt:
        mins = int((arr_dt - now).total_seconds() / 60)
        gps  = c(" 🟢", GREEN) if pos else ""
        if mins <= 5:
            return t, c(f"⬤ Arriving in {mins} min", YELLOW, BOLD) + gps
        return t, c(f"  Arriving {t}", GREEN) + gps

    return t, c("  Scheduled", DIM)


# ─── Main app ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Chicago Metra Schedule Viewer")
    parser.add_argument("--gtfs",  default="schedule.zip",
                        help="Path to GTFS zip or folder (default: schedule.zip)")
    parser.add_argument("--token", default="",
                        help="Metra API token for realtime mode")
    args = parser.parse_args()

    # ── Load static GTFS ──────────────────────────────────────────────────
    print(c("\n  Loading GTFS data…", DIM))
    try:
        gtfs = load_gtfs(args.gtfs)
    except Exception as e:
        print(c(f"\n  ERROR: {e}", RED))
        sys.exit(1)

    routes     = gtfs["routes.txt"]
    trips      = gtfs["trips.txt"]
    stop_times = gtfs["stop_times.txt"]
    stops      = gtfs["stops.txt"]
    calendar   = gtfs["calendar.txt"]

    stop_times["stop_sequence"] = pd.to_numeric(stop_times["stop_sequence"], errors="coerce")
    active_services = get_active_service_ids(calendar)

    # ── STEP 0: Choose mode ───────────────────────────────────────────────
    clear()
    header(
        "Chicago Metra Schedule Viewer",
        f"{chicago_today().strftime('%A, %B %d %Y')}  ·  "
        f"{chicago_now().strftime('%I:%M %p')} {tz_label()}"
    )

    print(c("  Select data mode:\n", WHITE, BOLD))
    print(f"  {c('1', YELLOW, BOLD)}.  {c('Static  ', WHITE, BOLD)}  "
          f"— Offline schedule only (no API key required)")
    print(f"  {c('2', YELLOW, BOLD)}.  {c('Realtime', GREEN, BOLD)}  "
          f"— Live train positions · delays · service alerts  "
          f"{c('(Metra API key required)', DIM)}")
    print()

    rt: Optional[RealtimeClient] = None

    while True:
        try:
            mode = input(c("  Mode ▶  ", CYAN)).strip()
        except KeyboardInterrupt:
            sys.exit(0)

        if mode == "1":
            print(c("\n  Static mode selected.\n", DIM))
            break

        if mode == "2":
            if not PROTO_OK:
                print(c("\n  ⚠  Protobuf library not installed. Run:\n", YELLOW))
                print(c("     pip install gtfs-realtime-bindings requests\n", DIM))
                continue

            token = args.token or input(c("  Enter your Metra API token ▶  ", CYAN)).strip()
            if not token:
                print(c("  Token cannot be empty.", RED))
                continue

            print(c("  Connecting to Metra realtime feeds…", DIM))
            rt = RealtimeClient(token)
            test = rt._fetch("alerts")
            if test is None:
                print(c("  ✘  Could not reach Metra API. "
                        "Check your token and internet connection.\n", RED))
                rt = None
                continue

            print(c("  ✔  Realtime mode active — feeds refresh every "
                    f"{RT_CACHE_TTL}s\n", GREEN, BOLD))
            break

        print(c("  Enter 1 or 2.", RED))

    mode_badge = (c("🟢 REALTIME", GREEN, BOLD)
                  if rt else c("📋 STATIC", DIM))

    # ── STEP 1+: Line selection wrapped in a loop so "Change Line" works ───
    while True:   # ← "Change Line" restarts here

        # ── STEP 1: Select line ───────────────────────────────────────────
        clear()
        header("Select a Metra Line", mode_badge)

        alerts = rt.get_alerts() if rt else []

        if alerts:
            print(c(f"  ⚠  {len(alerts)} active service alert(s):", YELLOW, BOLD))
            for a in alerts[:3]:
                print(c(f"     • {a['header']}", YELLOW))
            if len(alerts) > 3:
                print(c(f"     … and {len(alerts)-3} more", DIM))
            print()

        route_list = routes.sort_values("route_long_name").to_dict("records")

        def route_label(r):
            short = f"{c(r['route_short_name'], YELLOW, BOLD)}"
            flag  = ""
            if rt:
                ra = [a for a in alerts if r["route_id"] in a["route_ids"]]
                if ra:
                    flag = c("  ⚠ Alert", YELLOW)
            return f"{short:<20}  {r['route_long_name']}{flag}"

        choice = prompt_choice("Enter line number", route_list, route_label)
        selected_route = route_list[choice]
        route_id = selected_route["route_id"]

        # Route-specific alerts
        if rt:
            ra = [a for a in alerts if route_id in a["route_ids"]]
            if ra:
                print()
                print(c(f"  ⚠  Alert — {selected_route['route_short_name']}:", YELLOW, BOLD))
                for a in ra:
                    print(c(f"     {a['header']}", YELLOW))
                    if a["desc"]:
                        print(c(f"     {a['desc'][:120]}", DIM))
                print()

        # ── STEP 2: Build ordered station list ────────────────────────────
        route_trips = trips[
            (trips["route_id"] == route_id) &
            (trips["service_id"].isin(active_services))
        ]
        if route_trips.empty:
            print(c("  No service today for this line.", RED))
            print(c("  Press Enter to choose a different line.", DIM))
            input()
            continue  # back to line selection

        # ── Use ALL trips across ALL service IDs for this route so that
        #    stops only served on weekends / special services / seasonal
        #    schedules still appear in the station list (e.g. ME 95th St.).
        all_route_trips = trips[trips["route_id"] == route_id]
        ob_trips_all = all_route_trips[all_route_trips["direction_id"].astype(str).str.strip() == "0"]
        ib_trips_all = all_route_trips[all_route_trips["direction_id"].astype(str).str.strip() == "1"]
        if ob_trips_all.empty:
            ob_trips_all = all_route_trips

        # Outbound ordering (consistent geographic direction)
        all_ob_st = stop_times[stop_times["trip_id"].isin(ob_trips_all["trip_id"])]
        ob_order = (
            all_ob_st.groupby("stop_id")["stop_sequence"]
            .median()
            .reset_index()
            .rename(columns={"stop_sequence": "sort_seq"})
        )

        # Append inbound-only stops (branch stops not served outbound)
        if not ib_trips_all.empty:
            all_ib_st = stop_times[stop_times["trip_id"].isin(ib_trips_all["trip_id"])]
            ib_order = (
                all_ib_st.groupby("stop_id")["stop_sequence"]
                .median()
                .reset_index()
                .rename(columns={"stop_sequence": "sort_seq"})
            )
            ob_stop_ids = set(ob_order["stop_id"])
            extra = ib_order[~ib_order["stop_id"].isin(ob_stop_ids)].copy()
            ob_order = pd.concat([ob_order, extra], ignore_index=True)

        # ── Inject stops that exist in stops.txt but never appear in any
        #    trip's stop_times (e.g. UP-N Ravinia Park, ME 95th St.).
        #    Each entry: (stop_id, neighbour_before, neighbour_after)
        INJECT_STOPS = {
            "UP-N": [
                ("RAVINIAPK", "BRAESIDE", "RAVINIA"),   # Ravinia Park
            ],
            "ME": [
                ("95TH-UP",  "91ST-UP",  "103RD-UP"),  # 95th St., Chicago State Univ.
            ],
        }
        for inj_stop_id, before_id, after_id in INJECT_STOPS.get(route_id, []):
            # Only inject if the stop exists in stops.txt and is not already present
            if (inj_stop_id not in ob_order["stop_id"].values and
                    inj_stop_id in stops["stop_id"].values):
                before_rows = ob_order[ob_order["stop_id"] == before_id]
                after_rows  = ob_order[ob_order["stop_id"] == after_id]
                if not before_rows.empty and not after_rows.empty:
                    inj_seq = (float(before_rows["sort_seq"].iloc[0]) +
                               float(after_rows["sort_seq"].iloc[0])) / 2
                    inj_row = pd.DataFrame([{"stop_id": inj_stop_id, "sort_seq": inj_seq}])
                    ob_order = pd.concat([ob_order, inj_row], ignore_index=True)

        ordered_stops = (
            ob_order.sort_values("sort_seq")
            .merge(stops[["stop_id", "stop_name"]], on="stop_id", how="left")
            .reset_index(drop=True)
        )
        ordered_stops["stop_name"] = ordered_stops.apply(
            lambda r: r["stop_id"]
            if pd.isna(r["stop_name"]) or str(r["stop_name"]).strip() == ""
            else r["stop_name"], axis=1
        )

        # Keep today's route_trips for schedule filtering (active services only)
        ob_trips = route_trips[route_trips["direction_id"].astype(str).str.strip() == "0"]
        if ob_trips.empty:
            ob_trips = route_trips

        stop_records = ordered_stops.to_dict("records")

        # ── STEPS 3–5: Station selection → Schedule → Ladder ─────────────
        # Middle loop: "Change stations" restarts here within the same line.
        change_line = False

        while True:   # ← "Change stations" restarts here

            # ── STEP 3a: Departure station ────────────────────────────────
            clear()
            header(f"{selected_route['route_long_name']} — Select Departure Station",
                   mode_badge)
            print(c(f"  Line: {selected_route['route_short_name']}  "
                    f"{selected_route['route_long_name']}\n", CYAN))
            print(c("  Select your DEPARTURE station:", WHITE, BOLD))
            print()
            dep_idx = prompt_choice("Departure station number", stop_records,
                                    lambda r: r["stop_name"])

            # ── STEP 3b: Destination station (same order, dep highlighted) ─
            clear()
            header(f"{selected_route['route_long_name']} — Select Destination Station",
                   mode_badge)
            print(c(f"  From: {stop_records[dep_idx]['stop_name']}\n", GREEN, BOLD))
            print(c("  Select your DESTINATION station:", WHITE, BOLD))
            print(c("  (same order as departure list; your departure marked with ▶)\n", DIM))

            selectable = []
            for orig_i, sr in enumerate(stop_records):
                if orig_i == dep_idx:
                    print(f"  {c('  ▶', GREEN, BOLD)}  {c(sr['stop_name'], GREEN, BOLD)}  "
                          f"{c('← your departure', DIM)}")
                else:
                    selectable.append((orig_i, sr))
                    print(f"  {c(str(len(selectable)).rjust(3), YELLOW, BOLD)}.  {sr['stop_name']}")
            print()

            while True:
                try:
                    raw = input(c("  Destination station number ▶  ", CYAN)).strip()
                    n   = int(raw) - 1
                    if 0 <= n < len(selectable):
                        dest_record = selectable[n][1]
                        break
                    print(c(f"  Please enter 1–{len(selectable)}", RED))
                except (ValueError, KeyboardInterrupt):
                    print(c("  Invalid input. Try again.", RED))

            dep_stop_id  = stop_records[dep_idx]["stop_id"]
            dest_stop_id = dest_record["stop_id"]
            dep_name     = stop_records[dep_idx]["stop_name"]
            dest_name    = dest_record["stop_name"]

            # ── STEP 4: Train schedule ─────────────────────────────────────
            dep_times = (
                stop_times[stop_times["stop_id"] == dep_stop_id]
                [["trip_id", "departure_time", "stop_sequence"]]
                .rename(columns={"departure_time": "dep_time", "stop_sequence": "dep_seq"})
            )
            dest_times = (
                stop_times[stop_times["stop_id"] == dest_stop_id]
                [["trip_id", "arrival_time", "stop_sequence"]]
                .rename(columns={"arrival_time": "arr_time", "stop_sequence": "dest_seq"})
            )

            def build_schedule(rt_ids):
                m = dep_times.merge(dest_times, on="trip_id")
                m = m[m["dep_seq"] < m["dest_seq"]]
                m = m[m["trip_id"].isin(rt_ids)]
                m = m.merge(trips[["trip_id", "trip_headsign", "direction_id"]],
                            on="trip_id", how="left")
                m = m.sort_values("dep_time")
                m["train_no"] = m["trip_id"].apply(extract_train_no)
                return m

            W = {"no": 8, "dir": 11, "head": 20, "dep": 9, "arr": 9}

            def print_schedule_table(records, now_dt, pos_d, tu_d, is_today=True):
                print(c(
                    f"  {'#':>3}  {'Train':<{W['no']}}  {'Direction':<{W['dir']}}  "
                    f"{'Headsign':<{W['head']}}  {'Departs':>{W['dep']}}  "
                    f"{'Arrives':>{W['arr']}}  Status",
                    WHITE, BOLD
                ))
                print(c("  " + "─" * 90, DIM))
                for i, rec in enumerate(records, 1):
                    tid       = rec["trip_id"]
                    dep_dt, _ = parse_time(rec["dep_time"])
                    dep_str   = fmt_time(rec["dep_time"])
                    arr_str   = fmt_time(rec["arr_time"])
                    headsign  = (rec.get("trip_headsign") or "")[:W["head"]]
                    train_no  = rec.get("train_no", "—")
                    direction = direction_label(rec.get("direction_id", ""))
                    dir_str   = (c(f"⬆ {direction:<9}", GREEN)
                                 if "Inbound" in direction
                                 else c(f"⬇ {direction:<9}", YELLOW))
                    tu   = tu_d.get(tid, {})
                    pos  = pos_d.get(tid)
                    srel = tu.get("schedule_rel", 0)
                    if not is_today:
                        status = c("  Scheduled", GREEN)
                    elif srel == 3:
                        status = c("🚫 CANCELLED", RED, BOLD)
                    elif srel == 5:
                        status = c("➕ ADDED", GREEN)
                    elif dep_dt and dep_dt < now_dt:
                        if pos:
                            PS = {0: "🟢 Approaching", 1: "🟢 At stop", 2: "🟢 In transit"}
                            status = c(PS.get(pos.get("status", 2), "🟢 En route"), GREEN, BOLD)
                        elif tu:
                            status = c("📡 En route (no GPS)", DIM)
                        else:
                            status = c("✔ Departed", RED)
                    elif dep_dt:
                        mins = int((dep_dt - now_dt).total_seconds() / 60)
                        status = (c(f"⬤ In {mins} min", YELLOW, BOLD)
                                  if mins <= 5 else c("  Scheduled", GREEN))
                    else:
                        status = ""
                    dep_stu = tu.get("stop_updates", {}).get(dep_stop_id, {})
                    if dep_stu and not dep_stu.get("skipped") and dep_stu.get("dep_delay"):
                        dep_str += c(f" +{dep_stu['dep_delay']//60}m", RED)
                    num    = c(str(i).rjust(3), CYAN, BOLD)
                    no_str = c(f"{train_no:<{W['no']}}", WHITE, BOLD)
                    print(f"  {num}  {no_str}  {dir_str}  {headsign:<{W['head']}}  "
                          f"{dep_str:>{W['dep']}}  {arr_str:>{W['arr']}}  {status}")

            def show_options_schedule():
                print(c("  ┌─ Options ──────────────────────────────────────────────────────", CYAN))
                print(c("  │  Enter a train NUMBER → view ladder", CYAN))
                print(c("  │  D  → View schedule for a different date", CYAN))
                print(c("  │  C  → Change departure / destination stations", CYAN))
                print(c("  │  L  → Change line", CYAN))
                print(c("  │  0  → Exit", CYAN))
                print(c("  └────────────────────────────────────────────────────────────────", CYAN))
                print()

            positions    = rt.get_positions()    if rt else {}
            trip_updates = rt.get_trip_updates() if rt else {}
            fetch_time   = chicago_now()

            merged = build_schedule(route_trips["trip_id"].values)

            def show_schedule_screen(records, label_date, is_today, pos_d, tu_d):
                clear()
                now_dt = chicago_now() if is_today else datetime.combine(
                    label_date, datetime.min.time())
                header(f"Trains: {dep_name}  →  {dest_name}",
                       f"{label_date.strftime('%A, %B %d %Y')}  ·  {mode_badge}")
                line_info = (f"{selected_route['route_short_name']}  "
                             f"{selected_route['route_long_name']}")
                print(c(f"  Line    : {line_info}", CYAN))
                if is_today:
                    print(c(f"  Date    : {label_date.strftime('%A, %B %d %Y')}  ·  "
                            f"{chicago_now().strftime('%I:%M %p')} {tz_label()}\n", DIM))
                else:
                    print(c(f"  Date    : {label_date.strftime('%A, %B %d %Y')}  "
                            f"{c('(static — no realtime for future dates)', DIM)}\n", DIM))
                if not records:
                    print(c(f"  No trains found for this journey on "
                            f"{label_date.strftime('%A, %B %d %Y')}.", RED))
                    print()
                    return
                print_schedule_table(records, now_dt, pos_d, tu_d, is_today)
                print()
                if is_today and rt:
                    age = int((chicago_now() - fetch_time).total_seconds())
                    print(c(f"  🟢 Realtime data fetched {age}s ago · "
                            f"refreshes every {RT_CACHE_TTL}s", DIM))

            train_records  = merged.to_dict("records") if not merged.empty else []
            query_date     = chicago_today()
            sched_is_today = True
            pos_ctx        = positions
            tu_ctx         = trip_updates

            show_schedule_screen(train_records, query_date, sched_is_today,
                                 pos_ctx, tu_ctx)
            if not train_records:
                print(c("  No trains today.", YELLOW))
            show_options_schedule()

            # ── Schedule interaction loop ──────────────────────────────────
            change_stations = False
            selected_train  = None

            while True:
                try:
                    raw = input(c("  ▶  ", CYAN)).strip()
                except KeyboardInterrupt:
                    print(c("\n  Goodbye! 👋\n", CYAN))
                    sys.exit(0)

                if raw == "0":
                    print(c("\n  Goodbye! 👋\n", CYAN))
                    sys.exit(0)

                if raw.upper() == "L":
                    change_line = True
                    break

                if raw.upper() == "C":
                    change_stations = True
                    break

                if raw.upper() == "D":
                    print()
                    print(c("  Enter a date (e.g. 2025-12-25):", WHITE, BOLD))
                    while True:
                        date_raw = input(c("  Date (YYYY-MM-DD) ▶  ", CYAN)).strip()
                        try:
                            query_date = date.fromisoformat(date_raw)
                            break
                        except ValueError:
                            print(c("  Invalid format. Use YYYY-MM-DD.", RED))

                    weekday_q  = query_date.strftime("%A").lower()
                    date_int_q = int(query_date.strftime("%Y%m%d"))
                    svc_mask   = (
                        (calendar[weekday_q].astype(int) == 1) &
                        (calendar["start_date"].astype(int) <= date_int_q) &
                        (calendar["end_date"].astype(int) >= date_int_q)
                    )
                    active_svc_q = calendar[svc_mask]["service_id"].tolist()
                    rt_q = trips[
                        (trips["route_id"] == route_id) &
                        (trips["service_id"].isin(active_svc_q))
                    ]
                    merged_q = build_schedule(rt_q["trip_id"].values)
                    sched_is_today = (query_date == chicago_today())
                    pos_ctx = rt.get_positions()    if rt and sched_is_today else {}
                    tu_ctx  = rt.get_trip_updates() if rt and sched_is_today else {}
                    train_records = merged_q.to_dict("records") if not merged_q.empty else []
                    show_schedule_screen(train_records, query_date, sched_is_today,
                                         pos_ctx, tu_ctx)
                    if not train_records:
                        print(c("  No trains for that date.", YELLOW))
                    show_options_schedule()
                    continue

                try:
                    n = int(raw)
                    if 1 <= n <= len(train_records):
                        selected_train = train_records[n - 1]
                        break
                    print(c(f"  Enter 0, D, C, L, or 1–{len(train_records)}", RED))
                except ValueError:
                    print(c("  Invalid input. Enter a number, 0, D, C, or L.", RED))

            if change_line:
                break   # break stations loop → outer line loop restarts

            if change_stations:
                continue  # restart stations loop (same line)

            # ── STEP 5: Ladder view ────────────────────────────────────────
            while True:
                clear()
                trip_id   = selected_train["trip_id"]
                headsign  = selected_train.get("trip_headsign", trip_id)
                train_no  = selected_train.get("train_no", "—")
                direction = direction_label(selected_train.get("direction_id", ""))
                dir_arrow = "⬆" if "Inbound" in direction else "⬇"

                if rt:
                    positions    = rt.get_positions()
                    trip_updates = rt.get_trip_updates()

                tu   = trip_updates.get(trip_id, {})
                srel = tu.get("schedule_rel", 0)

                ladder_sub = mode_badge
                if srel == 3:
                    ladder_sub += "  " + c("🚫 THIS TRIP IS CANCELLED", RED, BOLD)
                elif srel == 5:
                    ladder_sub += "  " + c("➕ ADDED SERVICE", GREEN, BOLD)

                header(f"Ladder — Train {train_no}  {dir_arrow} {direction}", ladder_sub)
                line_info = (f"{selected_route['route_short_name']}  "
                             f"{selected_route['route_long_name']}")
                print(c(f"  Line      : {line_info}", CYAN))
                print(c(f"  Train No  : {train_no}", WHITE, BOLD))
                print(c(f"  Direction : {dir_arrow} {direction}", WHITE, BOLD))
                print(c(f"  To        : {headsign}", DIM))
                print(c(f"  Journey   : {dep_name}  →  {dest_name}\n", WHITE))

                trip_st = (
                    stop_times[stop_times["trip_id"] == trip_id]
                    .sort_values("stop_sequence")
                    .merge(stops[["stop_id", "stop_name"]], on="stop_id", how="left")
                )
                trip_st["stop_name"] = trip_st.apply(
                    lambda r: r["stop_id"]
                    if pd.isna(r["stop_name"]) or str(r["stop_name"]).strip() == ""
                    else r["stop_name"], axis=1
                )

                dep_seq_val  = int(selected_train["dep_seq"])
                dest_seq_val = int(selected_train["dest_seq"])
                seg_lo = min(dep_seq_val, dest_seq_val)
                seg_hi = max(dep_seq_val, dest_seq_val)

                all_rows   = trip_st.to_dict("records")
                total_rows = len(all_rows)
                max_name   = max((len(str(r.get("stop_name", ""))) for r in all_rows), default=20)
                max_name   = max(max_name, 20)

                print(c(f"  {'Station':<{max_name}}  {'Time':>9}  Status", WHITE, BOLD))
                print(c("  " + "─" * (max_name + 42), DIM))

                for idx, row in enumerate(all_rows):
                    sid       = row["stop_id"]
                    raw_name  = row.get("stop_name", "")
                    stop_name = sid if (not raw_name or str(raw_name) == "nan") else str(raw_name)
                    sched_arr = str(row.get("arrival_time", ""))
                    seq       = row.get("stop_sequence", 0)

                    is_dep   = sid == dep_stop_id
                    is_dest  = sid == dest_stop_id
                    in_seg   = seg_lo <= seq <= seg_hi
                    is_first = idx == 0
                    is_last  = idx == total_rows - 1

                    if is_first or is_last:
                        connector = c("  ○  ", DIM)
                    elif is_dep or is_dest:
                        connector = c("  ╠═", CYAN, BOLD)
                    elif in_seg:
                        connector = c("  ║  ", CYAN, BOLD)
                    else:
                        connector = c("  │  ", DIM)

                    if is_dep:    marker = c("▶ FROM", GREEN, BOLD)
                    elif is_dest: marker = c("★ TO  ", YELLOW, BOLD)
                    elif in_seg:  marker = c("  •   ", WHITE)
                    else:         marker = c("  ·   ", DIM)

                    stu = tu.get("stop_updates", {}).get(sid, {})
                    if not in_seg and not is_dep and not is_dest:
                        time_disp  = fmt_time(sched_arr)
                        status_str = ""
                        name_col   = c(f"{stop_name:<{max_name}}", DIM)
                        time_col   = c(f"{time_disp:>9}", DIM)
                    elif stu.get("skipped"):
                        time_disp  = fmt_time(sched_arr)
                        status_str = c("⛔ Skipped", RED)
                        name_col   = c(f"{stop_name:<{max_name}}", DIM)
                        time_col   = c(f"{time_disp:>9}", DIM)
                    else:
                        time_disp, status_str = stop_rt_status(
                            trip_id, sid, rt, positions, trip_updates, sched_arr
                        )
                        departed = "Departed" in status_str
                        if is_dep:
                            name_col = c(f"{stop_name:<{max_name}}", GREEN, BOLD)
                            time_col = c(f"{time_disp:>9}", GREEN, BOLD)
                        elif is_dest:
                            name_col = c(f"{stop_name:<{max_name}}", YELLOW, BOLD)
                            time_col = c(f"{time_disp:>9}", YELLOW, BOLD)
                        elif departed:
                            name_col = c(f"{stop_name:<{max_name}}", DIM)
                            time_col = c(f"{time_disp:>9}", DIM)
                        else:
                            name_col = c(f"{stop_name:<{max_name}}", WHITE)
                            time_col = c(f"{time_disp:>9}", WHITE)

                    print(f"{connector} {marker}  {name_col}  {time_col}  {status_str}")

                print()
                if rt:
                    pos = positions.get(trip_id)
                    if pos:
                        PS = {0: "Approaching next stop", 1: "At a stop", 2: "In transit"}
                        ps = PS.get(pos.get("status", 2), "En route")
                        lat, lon = pos.get("lat"), pos.get("lon")
                        coord = f"  ({lat:.4f}, {lon:.4f})" if lat and lon else ""
                        print(c(f"  🟢 Live GPS: {ps}{coord}", GREEN))
                    else:
                        dep_dt, _ = parse_time(selected_train.get("dep_time", ""))
                        if dep_dt and dep_dt < chicago_now():
                            print(c("  📡 Train en route — no GPS (underground or terminal)", DIM))
                    print()

                legend = (c("✔ Departed", RED) + "  " +
                          c("⬤ Arriving soon", YELLOW, BOLD) + "  " +
                          c("Arriving HH:MM", GREEN))
                if rt:
                    legend += "  " + c("🟢 Live GPS", GREEN) + "  " + c("⛔ Skipped", RED)
                print(c("  Legend: ", DIM) + legend)
                print()
                print(c("  ┌─ Options ──────────────────────────────────────────────────────", CYAN))
                print(c("  │  C  → Change departure / destination stations", CYAN))
                print(c("  │  L  → Change line", CYAN))
                print(c("  │  0  → Exit", CYAN))
                print(c("  └────────────────────────────────────────────────────────────────", CYAN))
                print()

                while True:
                    try:
                        ladder_raw = input(c("  ▶  ", CYAN)).strip().upper()
                    except KeyboardInterrupt:
                        print(c("\n  Goodbye! 👋\n", CYAN))
                        sys.exit(0)
                    if ladder_raw == "0":
                        print(c("\n  Goodbye! 👋\n", CYAN))
                        sys.exit(0)
                    if ladder_raw in ("C", "L"):
                        break
                    print(c("  Enter C, L, or 0.", RED))

                if ladder_raw == "L":
                    change_line = True
                break   # always break ladder inner loop

            if change_line:
                break   # break stations loop → outer line loop restarts

            # C pressed from ladder → restart stations loop (same line)
            # (ladder_raw == "C" falls through here)

        # change_line == True → outer while True restarts (new line selection)


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
