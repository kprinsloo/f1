#!/usr/bin/env python3
"""F1 points tracker: rebuild one season's per-round and cumulative points from F1DB.

Usage:  python3 update.py [SEASON] [--force]   (defaults to the current year)

Request limits (so the tracker stays a polite, low-volume client of F1DB, GitHub and OpenF1):
- Between race weekends a run makes no requests at all: unless a race was held in the last
  QUIET_DAYS days (default 10) or a past race still has no results, it stops straight away.
- Otherwise it first asks GitHub for the latest F1DB commit touching this season (1 request) and
  stops if nothing changed since the last run.
- Without git, F1DB files are kept in a cache and only files changed since the last run are
  downloaded again (via one GitHub "compare" request); future races only need their race.yml.
- Every request is counted, a run stops after MAX_REQUESTS (default 400), and the totals per site
  are printed at the end. --force (or FORCE=1) skips the first two checks.

Source: F1DB (https://github.com/f1db/f1db), pulled with a sparse git checkout when
git is installed, otherwise downloaded file by file. Needs only standard Python.
Every run rebuilds the whole season from source, so it is idempotent: re-running
after a steward penalty or a points correction simply produces corrected data.
Changes versus the previous run are appended to data/<season>/changelog.json.
"""
import csv, datetime, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time
import atexit, threading, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    import yaml
except ImportError:  # PyYAML is optional; F1DB's files are simple enough for the reader below.
    yaml = None

HERE = os.path.dirname(os.path.abspath(__file__))
# Scratch copy of the F1DB files. Kept in the system temp folder, not next to the script: in a
# OneDrive (or other synced) folder the sync client locks these files and the clean-up fails.
CACHE = os.environ.get("F1DB_CACHE", os.path.join(tempfile.gettempdir(), "f1-points-tracker-f1db"))
REPO = "https://github.com/f1db/f1db.git"
API = "https://api.github.com/repos/f1db/f1db"
FORCE = "--force" in sys.argv or os.environ.get("FORCE") == "1"
QUIET_DAYS = int(os.environ.get("QUIET_DAYS", "10"))
MAX_REQUESTS = int(os.environ.get("MAX_REQUESTS", "400"))
REQUESTS, _LOCK = {}, threading.Lock()


@atexit.register
def _report_requests():
    total = sum(REQUESTS.values())
    print(f"requests this run: {total}" + (f" ({', '.join(f'{h} {n}' for h, n in sorted(REQUESTS.items()))})" if total else ""))

# Team colours, keyed by F1DB constructorId. Unknown teams fall back to grey.
TEAM_COLORS = {
    "mercedes": "#27B9AD", "ferrari": "#E8002D", "mclaren": "#FF8000",
    "red-bull": "#3671C6", "racing-bulls": "#8FA8FF", "aston-martin": "#229971",
    "alpine": "#FF87BC", "williams": "#64C4FF", "haas": "#B6BABD",
    "audi": "#52E252", "cadillac": "#C9A227", "kick-sauber": "#52E252", "rb": "#8FA8FF",
}


def sh(*args, cwd=None):
    try:
        subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as e:
        sys.exit(f"'{' '.join(args)}' failed:\n{e.stderr.strip()}")


def sync_git(season):
    paths = [f"src/data/seasons/{season}", "src/data/drivers", "src/data/constructors", "src/data/grands-prix"]
    if not os.path.isdir(os.path.join(CACHE, ".git")):
        sh("git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", REPO, CACHE)
    else:
        sh("git", "fetch", "--depth", "1", "origin", "main", cwd=CACHE)
        sh("git", "reset", "--hard", "origin/main", cwd=CACHE)
    sh("git", "sparse-checkout", "set", *paths, cwd=CACHE)
    return CACHE, subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=CACHE, text=True).strip()


def fetch(url, accept=None):
    """GET a URL; returns bytes, or None on 404. Every call is counted against MAX_REQUESTS."""
    host = urllib.parse.urlsplit(url).hostname
    with _LOCK:
        if sum(REQUESTS.values()) >= MAX_REQUESTS:
            raise RuntimeError(f"request limit of {MAX_REQUESTS} reached, stopping")
        REQUESTS[host] = REQUESTS.get(host, 0) + 1
    headers = {"User-Agent": "f1-points-tracker", **({"Accept": accept} if accept else {})}
    if host == "api.github.com" and os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


RACE_FILES = ["race.yml", "race-results.yml", "sprint-race-results.yml", "driver-standings.yml",
              "constructor-standings.yml", "qualifying-results.yml", "fastest-laps.yml", "pit-stops.yml"]


def sync_http(season):
    """Keep a local copy of just the files the tracker reads, straight from GitHub (no git needed).

    The copy is kept between runs. When it exists, one "compare" request lists the files F1DB
    changed since then and only those are downloaded; otherwise everything is fetched once."""
    commit = fetch(f"{API}/commits/main", "application/vnd.github.sha").decode().strip()
    raw = f"https://raw.githubusercontent.com/f1db/f1db/{commit}/src/data"
    root = os.path.join(CACHE, "http")
    base = os.path.join(root, "src", "data")
    stamp = os.path.join(root, ".commit")
    prev = open(stamp).read().strip() if os.path.exists(stamp) else None
    races_rel = f"seasons/{season}/races"

    def local(rel):
        return os.path.join(base, *rel.split("/"))

    def get(rel):
        data = fetch(f"{raw}/{rel}")
        path = local(rel)
        if data is None:
            if os.path.exists(path):
                os.remove(path)
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return data

    with ThreadPoolExecutor(4) as pool:
        changed = None
        if prev == commit and os.path.isdir(os.path.join(base, races_rel)):
            changed = []
        elif prev and os.path.isdir(os.path.join(base, races_rel)):
            try:
                cmp = json.loads(fetch(f"{API}/compare/{prev}...{commit}"))
                if cmp.get("status") in ("ahead", "identical") and len(cmp.get("files", [])) < 300:
                    changed = cmp["files"]
            except Exception as e:
                print(f"  F1DB: compare failed ({e}), downloading everything")
        if changed is None:
            # Full download. Start clean so files F1DB removed don't linger; a locked file (virus
            # scanner, sync client) must not stop the update.
            shutil.rmtree(root, ignore_errors=True)
            listing = fetch(f"{API}/contents/src/data/{races_rel}?ref={commit}")
            if listing is None:
                sys.exit(f"No F1DB data for season {season} yet")
            folders = [e["name"] for e in json.loads(listing) if e["type"] == "dir"]
            list(pool.map(get, [f"{races_rel}/{f}/race.yml" for f in folders]))
            # Races that haven't happened yet only have race.yml, so don't ask for the rest.
            soon = datetime.date.today() + datetime.timedelta(days=1)
            past = [f for f in folders
                    if datetime.date.fromisoformat(str(load(local(f"{races_rel}/{f}/race.yml"), {}).get("date", "9999-12-31"))) <= soon]
            list(pool.map(get, [f"{races_rel}/{f}/{n}" for f in past for n in RACE_FILES if n != "race.yml"]))
        else:
            todo = []
            for c in changed:
                for name, gone in ((c.get("previous_filename"), True), (c["filename"], c["status"] == "removed")):
                    if not name or not name.startswith("src/data/"):
                        continue
                    rel = name[len("src/data/"):]
                    relevant = (rel.startswith(races_rel + "/") and rel.rsplit("/", 1)[-1] in RACE_FILES) or \
                               (rel.split("/")[0] in ("drivers", "constructors", "grands-prix") and os.path.exists(local(rel)))
                    if not relevant:
                        continue
                    if gone:
                        if os.path.exists(local(rel)):
                            os.remove(local(rel))
                    else:
                        todo.append(rel)
            list(pool.map(get, todo))

        # Make sure every grand prix, driver and team the results mention is in the copy.
        gps, drv, con = set(), set(), set()
        rdir = os.path.join(base, races_rel)
        for folder in os.listdir(rdir):
            for name in ("race.yml", "race-results.yml", "sprint-race-results.yml"):
                path = os.path.join(rdir, folder, name)
                if not os.path.exists(path):
                    continue
                text = open(path, encoding="utf-8").read()
                if name == "race.yml":
                    gps.update(re.findall(r"^grandPrixId:\s*(\S+)", text, re.M))
                else:
                    drv.update(re.findall(r"driverId:\s*(\S+)", text))
                    con.update(re.findall(r"constructorId:\s*(\S+)", text))
        meta = [f"grands-prix/{g}.yml" for g in gps] + [f"drivers/{d}.yml" for d in drv] + [f"constructors/{c}.yml" for c in con]
        list(pool.map(get, [m for m in meta if not os.path.exists(local(m))]))
    with open(stamp, "w") as f:
        f.write(commit)
    return root, commit


def sync_source(season):
    return sync_git(season) if shutil.which("git") else sync_http(season)


def _scalar(v):
    v = v.strip()
    if v in ("", "~", "null"):
        return None
    if v[0] == '"':
        return json.loads(v)
    if v[0] == "'":
        return v[1:-1].replace("''", "'")
    if v in ("true", "false"):
        return v == "true"
    if re.fullmatch(r"[-+]?\d+", v):
        return int(v)
    if re.fullmatch(r"[-+]?\d*\.\d+", v):
        return float(v)
    return v


def _parse(lines, i, ind):
    """Block-style YAML (maps, lists, scalars) starting at lines[i], indented by ind."""
    out = [] if lines[i][1].startswith("-") else {}
    while i < len(lines) and lines[i][0] == ind:
        text = lines[i][1]
        if isinstance(out, list):
            if not text.startswith("-"):
                break
            item = text[1:].strip()
            if not item:
                val, i = _parse(lines, i + 1, lines[i + 1][0])
            elif re.match(r"[^\s\"']+:(\s|$)", item):
                # "- key: value" starts a map whose keys sit two columns in.
                lines[i] = (ind + 2, item)
                val, i = _parse(lines, i, ind + 2)
            else:
                val, i = _scalar(item), i + 1
            out.append(val)
        else:
            if text.startswith("-"):
                break
            key, _, rest = text.partition(":")
            i += 1
            if rest.strip():
                out[key.strip()] = _scalar(rest)
            elif i < len(lines) and (lines[i][0] > ind or (lines[i][0] == ind and lines[i][1].startswith("-"))):
                out[key.strip()], i = _parse(lines, i, lines[i][0])
            else:
                out[key.strip()] = None
    return out, i


def parse_yaml(text):
    lines = [(len(l) - len(l.lstrip(" ")), l.strip()) for l in text.splitlines()
             if l.strip() and not l.lstrip().startswith("#")]
    return _parse(lines, 0, lines[0][0])[0] if lines else None


def load(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        text = f.read()
    data = yaml.safe_load(text) if yaml else parse_yaml(text)
    return data or default


def seconds(t):
    """'23.062' or '1:01.177' -> seconds as a float."""
    if t is None:
        return None
    m, _, s = str(t).rpartition(":")
    return round((int(m) * 60 if m else 0) + float(s), 3)


def build(season, root, commit):
    base = os.path.join(root, "src", "data")
    sdir = os.path.join(base, "seasons", str(season))
    rdir = os.path.join(sdir, "races")
    if not os.path.isdir(rdir):
        sys.exit(f"No F1DB data for season {season} yet")

    rounds, drows, crows, warnings = [], [], [], []
    drivers, constructors = {}, {}
    dcum, ccum = {}, {}

    for folder in sorted(os.listdir(rdir)):
        f = os.path.join(rdir, folder)
        race = load(os.path.join(f, "race.yml"), {})
        gp = load(os.path.join(base, "grands-prix", f"{race['grandPrixId']}.yml"), {})
        results = load(os.path.join(f, "race-results.yml"))
        sprint = load(os.path.join(f, "sprint-race-results.yml"))
        has_sprint = sprint is not None or bool(race.get("sprintRaceDate"))
        done = results is not None
        rounds.append({
            "round": race["round"], "gpId": race["grandPrixId"],
            "name": gp.get("fullName", race["grandPrixId"]), "short": gp.get("shortName", race["grandPrixId"]),
            "abbr": gp.get("abbreviation", race["grandPrixId"][:3].upper()),
            "date": str(race["date"]), "sprint": has_sprint, "completed": done,
        })
        if not done:
            continue
        rnd = race["round"]

        # Points scored this weekend, per driver (attributed to the team they drove for).
        wk = {}
        for kind, rows in (("race", results), ("sprint", sprint or [])):
            for r in rows:
                d = wk.setdefault(r["driverId"], {"constructorId": r["constructorId"], "race": 0, "sprint": 0,
                                                  "racePos": None, "sprintPos": None, "grid": None, "retired": None})
                d[kind] += r.get("points") or 0
                d[f"{kind}Pos"] = r.get("position") or r.get("positionText")
                if kind == "race":
                    d["constructorId"] = r["constructorId"]
                    # "PL" = started from the pit lane, i.e. behind everyone on the grid.
                    g = r.get("gridPosition")
                    d["grid"] = len(results) if g == "PL" else g
                    d["retired"] = r.get("reasonRetired")
                drivers.setdefault(r["driverId"], None)
                constructors.setdefault(r["constructorId"], None)

        # Qualifying, fastest lap and pit stops. Pit stop times are F1DB's pit lane time (entry to exit).
        quali = {q["driverId"]: q.get("position") for q in load(os.path.join(f, "qualifying-results.yml"), [])}
        laps = load(os.path.join(f, "fastest-laps.yml"), [])
        fastest = next((l["driverId"] for l in laps if l.get("position") == 1), None)
        pits = {}
        for p in load(os.path.join(f, "pit-stops.yml"), []):
            if seconds(p.get("time")) is not None:
                pits.setdefault(p["driverId"], []).append(seconds(p["time"]))

        dstand = {s["driverId"]: s for s in load(os.path.join(f, "driver-standings.yml"), [])}
        cstand = {s["constructorId"]: s for s in load(os.path.join(f, "constructor-standings.yml"), [])}

        cwk = {}
        for did, d in wk.items():
            pts = d["race"] + d["sprint"]
            dcum[did] = dcum.get(did, 0) + pts
            c = cwk.setdefault(d["constructorId"], {"race": 0, "sprint": 0})
            c["race"] += d["race"]; c["sprint"] += d["sprint"]
        # Drivers who sat this round out keep their cumulative total on the trend line.
        for did in list(dcum):
            if did not in wk:
                wk[did] = None
        for did, d in wk.items():
            st = dstand.get(did, {})
            official = st.get("points")
            if official is not None and abs(official - dcum[did]) > 1e-9:
                warnings.append(f"R{rnd} {did}: computed {dcum[did]} vs official {official}; using official")
                dcum[did] = official
            drows.append({
                "round": rnd, "driverId": did,
                "constructorId": d["constructorId"] if d else None,
                "racePoints": d["race"] if d else 0, "sprintPoints": d["sprint"] if d else 0,
                "points": (d["race"] + d["sprint"]) if d else 0,
                "racePos": d["racePos"] if d else None, "sprintPos": d["sprintPos"] if d else None,
                "cumulative": dcum[did], "standing": st.get("position"), "started": d is not None,
                "qualiPos": quali.get(did), "grid": d["grid"] if d else None,
                "retired": d["retired"] if d else None, "fastestLap": did == fastest,
                "pitStops": len(pits.get(did, [])), "pitBest": min(pits[did]) if pits.get(did) else None,
                "pitTotal": round(sum(pits[did]), 3) if pits.get(did) else None,
            })
        for cid in sorted(set(ccum) | set(cwk)):
            c = cwk.get(cid, {"race": 0, "sprint": 0})
            ccum[cid] = ccum.get(cid, 0) + c["race"] + c["sprint"]
            st = cstand.get(cid, {})
            official = st.get("points")
            if official is not None and abs(official - ccum[cid]) > 1e-9:
                warnings.append(f"R{rnd} {cid}: computed {ccum[cid]} vs official {official}; using official")
                ccum[cid] = official
            crows.append({
                "round": rnd, "constructorId": cid, "racePoints": c["race"], "sprintPoints": c["sprint"],
                "points": c["race"] + c["sprint"], "cumulative": ccum[cid], "standing": st.get("position"),
            })

    for did in drivers:
        m = load(os.path.join(base, "drivers", f"{did}.yml"), {})
        drivers[did] = {"id": did, "name": m.get("name", did), "abbr": m.get("abbreviation", did[:3].upper()),
                        "number": m.get("permanentNumber"), "nationality": m.get("nationalityCountryId")}
    for cid in constructors:
        m = load(os.path.join(base, "constructors", f"{cid}.yml"), {})
        constructors[cid] = {"id": cid, "name": m.get("name", cid), "color": TEAM_COLORS.get(cid, "#888888")}
    # A driver's current team is the team of their most recent start.
    for r in drows:
        if r["constructorId"]:
            drivers[r["driverId"]]["team"] = r["constructorId"]

    return {
        "season": season,
        "source": {"name": "F1DB", "url": "https://github.com/f1db/f1db", "commit": commit},
        "rounds": rounds, "drivers": list(drivers.values()), "constructors": list(constructors.values()),
        "driverRounds": drows, "constructorRounds": crows, "warnings": warnings,
    }


def diff(old, new):
    """Point changes for rounds that existed in the previous run (i.e. corrections)."""
    if not old:
        return []
    key = lambda r: (r["round"], r.get("driverId") or r.get("constructorId"))
    out = []
    for table in ("driverRounds", "constructorRounds"):
        prev = {key(r): r for r in old.get(table, [])}
        for r in new[table]:
            p = prev.get(key(r))
            if p and (p["points"], p["cumulative"]) != (r["points"], r["cumulative"]):
                out.append({"round": r["round"], "id": key(r)[1], "table": table,
                            "points": [p["points"], r["points"]], "cumulative": [p["cumulative"], r["cumulative"]]})
    return out


def dashboard_rows(data):
    """Flat, denormalised rows for the dashboard: one per entity per completed round."""
    rnd = {r["round"]: r for r in data["rounds"]}
    drv = {d["id"]: d for d in data["drivers"]}
    con = {c["id"]: c for c in data["constructors"]}

    def enrich(rows, entity):
        out, wins, pods = [], {}, {}
        for r in sorted(rows, key=lambda r: (r["round"], r["standing"] or 99)):
            eid = r[entity]
            lead = max(x["cumulative"] for x in rows if x["round"] == r["round"])
            row = {"round": r["round"], "gp": rnd[r["round"]]["abbr"], "gpName": rnd[r["round"]]["short"],
                   "date": rnd[r["round"]]["date"], "sprintWeekend": rnd[r["round"]]["sprint"]}
            if entity == "driverId":
                d = drv[eid]
                tid = r["constructorId"] or d.get("team")
                p = r["racePos"] if isinstance(r["racePos"], int) else None
                wins[eid] = wins.get(eid, 0) + (p == 1)
                pods[eid] = pods.get(eid, 0) + (p is not None and p <= 3)
                row.update({"driverId": eid, "driver": d["name"], "abbr": d["abbr"],
                            "teamId": tid, "team": con[tid]["name"], "color": con[tid]["color"],
                            "racePos": r["racePos"], "raced": r["started"],
                            "sprintPos": r["sprintPos"], "wins": wins[eid], "podiums": pods[eid],
                            "qualiPos": r["qualiPos"], "grid": r["grid"],
                            "gained": r["grid"] - p if p is not None and isinstance(r["grid"], int) else None,
                            "retired": r["retired"], "fastestLap": r["fastestLap"],
                            "pitStops": r["pitStops"], "pitBest": r["pitBest"], "pitTotal": r["pitTotal"]})
            else:
                row.update({"teamId": eid, "team": con[eid]["name"], "color": con[eid]["color"]})
            row.update({"racePoints": r["racePoints"], "sprintPoints": r["sprintPoints"], "points": r["points"],
                        "cumulative": r["cumulative"], "standing": r["standing"],
                        "gapToLeader": lead - r["cumulative"]})
            out.append(row)
        return out

    rounds = [{"round": r["round"], "gp": r["abbr"], "gpName": r["short"], "name": r["name"], "date": r["date"],
               "sprintWeekend": r["sprint"], "completed": r["completed"]} for r in data["rounds"]]
    return enrich(data["driverRounds"], "driverId"), enrich(data["constructorRounds"], "constructorId"), rounds


def write_csv(path, rows):
    if not rows:  # a new season before its first race
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)


def write_season_index():
    """data/seasons.json lists every season that has data, newest first, for the web page."""
    root = os.path.join(HERE, "data")
    seasons = sorted((int(n) for n in os.listdir(root)
                      if n.isdigit() and os.path.exists(os.path.join(root, n, "dash_rounds.json"))), reverse=True)
    with open(os.path.join(root, "seasons.json"), "w", encoding="utf-8") as f:
        json.dump(seasons, f)


# ---------------------------------------------------------------------------------------------
# Lap by lap: every driver's position and lap time on every lap, from OpenF1 (https://openf1.org).
# F1DB has no lap-level data. Each finished race is downloaded once and saved to
# data/<season>/laps/<round>.json; races already saved are skipped, so a normal run makes one
# request (the season's race list) or none. A race costs two requests (laps and positions), and
# requests are spaced to stay well inside OpenF1's free limits (3 a second, 30 a minute).
# OpenF1 refuses free requests while a session is live; that, or any other failure, only prints
# a warning and the next run tries again. The points tracker itself never depends on OpenF1.
OPENF1 = "https://api.openf1.org/v1"
LAPS_BUDGET = int(os.environ.get("LAPS_BUDGET", "60"))
# Wall-clock cap for the lap downloads, so a slow API can't hold up the run.
LAPS_MINUTES = float(os.environ.get("LAPS_MINUTES", "5"))
GAP = 2.5  # seconds between requests: 24 a minute
# A race whose lap data OpenF1 still hasn't got after this many days is no longer retried (unless
# --force, or the laps folder is empty, i.e. a first run that fills in the season).
LAP_RETRY_DAYS = int(os.environ.get("LAP_RETRY_DAYS", "30"))
LAPS_VERSION = 2  # bump when the lap file gains fields; older files are downloaded again


def lap_file_current(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("v", 1) >= LAPS_VERSION
    except (OSError, ValueError):
        return False


class Budget(Exception):
    pass


def openf1(path, budget):
    """GET one OpenF1 endpoint as JSON, backing off once on rate limits. Spends one unit of budget."""
    if budget[0] <= 0 or time.monotonic() > budget[1]:
        raise Budget()
    budget[0] -= 1
    for attempt in range(3):
        try:
            time.sleep(GAP)
            data = fetch(f"{OPENF1}/{path}")
            return json.loads(data) if data else []
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise RuntimeError("OpenF1 refused the request (it blocks free access while a session is live)")
            if e.code != 429 and e.code < 500:
                raise
            wait = min(int(e.headers.get("Retry-After") or 0) or 30 * (attempt + 1), 60)
            print(f"  laps: OpenF1 returned {e.code}, waiting {wait}s", flush=True)
        except (urllib.error.URLError, TimeoutError) as e:
            wait = 10 * (attempt + 1)
            print(f"  laps: {type(e).__name__} from OpenF1, waiting {wait}s", flush=True)
        if time.monotonic() + wait > budget[1]:
            raise Budget()
        time.sleep(wait)
    raise RuntimeError(f"OpenF1 kept failing for {path}")


def _when(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def race_laps(session, folder, race, data, budget):
    """Build one race's lap file, or None if OpenF1 doesn't have the full race yet."""
    rnd = race["round"]
    f1db_results = load(os.path.join(folder, "race-results.yml"), [])
    by_number = {r.get("driverNumber"): r for r in f1db_results}
    drv = {d["id"]: d for d in data["drivers"]}
    con = {c["id"]: c for c in data["constructors"]}

    key = session["session_key"]
    laps = openf1(f"laps?session_key={key}", budget)
    winner_laps = max((r.get("laps") or 0 for r in f1db_results), default=0)
    n = max((l.get("lap_number") or 0 for l in laps), default=0)
    if not laps or n < winner_laps:
        return None  # not published yet, or still partial: try again next run
    n = winner_laps or n
    positions = openf1(f"position?session_key={key}", budget)

    # Position changes, per car, in time order.
    changes = {}
    for p in sorted(positions, key=lambda p: p["date"]):
        changes.setdefault(p["driver_number"], []).append((_when(p["date"]), p["position"]))

    rows = {}
    for num, mine in sorted(_group(laps, "driver_number").items()):
        did = by_number.get(num, {}).get("driverId")
        if not did:
            continue
        mine = {l["lap_number"]: l for l in mine if l.get("lap_number")}
        row = rows.setdefault(did, {"pos": [None] * n, "time": [None] * n, "end": [None] * n})
        hist = changes.get(num, [])
        for lap in range(1, n + 1):
            l = mine.get(lap)
            if not l:
                continue
            row["time"][lap - 1] = round(l["lap_duration"], 3) if l.get("lap_duration") else None
            # Position as the car crossed the line: the latest change at or before the lap's end.
            nxt = mine.get(lap + 1, {}).get("date_start")
            end = _when(nxt) if nxt else (_when(l["date_start"]) + datetime.timedelta(seconds=l["lap_duration"])
                                          if l.get("date_start") and l.get("lap_duration") else None)
            if end is None:
                continue
            row["end"][lap - 1] = end
            pos = None
            for t, p in hist:
                if t > end + datetime.timedelta(seconds=1):
                    break
                pos = p
            row["pos"][lap - 1] = pos

    pits = {}
    for p in load(os.path.join(folder, "pit-stops.yml"), []):
        pits.setdefault(p["driverId"], []).append(p.get("lap"))

    rnd_info = next(r for r in data["rounds"] if r["round"] == rnd)
    laps_done = {r["driverId"]: r.get("laps") or 0 for r in f1db_results}
    first = [min((row["end"][i] for did, row in rows.items() if row["end"][i] and laps_done.get(did, 0) > i),
                 default=None) for i in range(n)]
    out = []
    for r in f1db_results:  # finishing order
        did = r["driverId"]
        if did not in rows:
            continue
        cid = r["constructorId"]
        g = r.get("gridPosition")
        # Trim laps after a retirement, where OpenF1 can still report a stale position.
        done = r.get("laps") or 0
        pos, tim, end = rows[did]["pos"], rows[did]["time"], rows[did]["end"]
        pos[done:] = [None] * (n - done); tim[done:] = [None] * (n - done); end[done:] = [None] * (n - done)
        # Gap to the leader: how long after the first car this one finished the same lap. For a
        # lapped car that is a true time gap (more than a lap's worth), not "+1 lap".
        gap = [round((e - first[i]).total_seconds(), 3) if e and first[i] else None for i, e in enumerate(end)]
        out.append({"driverId": did, "abbr": drv[did]["abbr"], "name": drv[did]["name"],
                    "teamId": cid, "team": con[cid]["name"], "color": con[cid]["color"],
                    "finish": r.get("position") or r.get("positionText"),
                    "grid": len(f1db_results) if g == "PL" else g,
                    "pits": sorted(l for l in pits.get(did, []) if l), "pos": pos, "time": tim, "gap": gap})
    return {"v": LAPS_VERSION, "round": rnd, "gp": rnd_info["abbr"], "gpName": rnd_info["short"], "laps": n,
            "source": {"name": "OpenF1", "url": "https://openf1.org"}, "drivers": out}


def _group(rows, key):
    out = {}
    for r in rows:
        out.setdefault(r.get(key), []).append(r)
    return out


def laps_wanted(out, rounds):
    """Completed rounds whose lap file is missing or out of date and still worth asking OpenF1 for."""
    backfill = FORCE or not any(re.fullmatch(r"\d+\.json", n) for n in (os.listdir(out) if os.path.isdir(out) else []))
    today = datetime.date.today()
    return {r["round"] for r in rounds
            if r["completed"] and not lap_file_current(os.path.join(out, f"{r['round']:02d}.json"))
            and (backfill or (today - datetime.date.fromisoformat(str(r["date"]))).days <= LAP_RETRY_DAYS)}


def update_laps(season, root, data):
    rdir = os.path.join(root, "src", "data", "seasons", str(season), "races")
    out = os.path.join(HERE, "data", str(season), "laps")
    wanted = laps_wanted(out, data["rounds"])
    os.makedirs(out, exist_ok=True)
    budget, added = [LAPS_BUDGET, time.monotonic() + LAPS_MINUTES * 60], []
    try:
        todo = []
        for name in sorted(os.listdir(rdir)):
            folder = os.path.join(rdir, name)
            race = load(os.path.join(folder, "race.yml"), {})
            if race.get("round") in wanted and os.path.exists(os.path.join(folder, "race-results.yml")):
                todo.append((folder, race))
        if todo:
            sessions = openf1(f"sessions?year={season}&session_name=Race", budget)
            for folder, race in todo:
                # F1DB dates are local; OpenF1 start times are UTC, so allow a day either way.
                day = datetime.date.fromisoformat(str(race["date"]))
                match = [s for s in sessions if s.get("date_start")
                         and abs((_when(s["date_start"]).date() - day).days) <= 1]
                if len(match) != 1:
                    print(f"  laps: round {race['round']} has no OpenF1 race session yet")
                    continue
                laps = race_laps(match[0], folder, race, data, budget)
                if laps is None:
                    print(f"  laps: round {race['round']} not complete on OpenF1 yet")
                    continue
                with open(os.path.join(out, f"{race['round']:02d}.json"), "w", encoding="utf-8") as f:
                    json.dump(laps, f, separators=(",", ":"))
                added.append(race["round"])
                print(f"  laps: saved round {race['round']} ({LAPS_BUDGET - budget[0]} requests so far)", flush=True)
    except Budget:
        print(f"  laps: time or request limit reached, the rest will follow next run")
    except Exception as e:  # never let lap data break the points update
        print(f"  laps: skipped ({type(e).__name__}: {e})")
    have = sorted(int(n[:-5]) for n in os.listdir(out) if re.fullmatch(r"\d+\.json", n))
    with open(os.path.join(out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(have, f)
    print(f"{season}: lap data for {len(have)} races{f', added rounds {added}' if added else ''}")


def nothing_due(old, laps_dir, laps_on):
    """True between race weekends: no race in the last QUIET_DAYS days, every past race has results
    and lap data. Then there is nothing new to fetch and the run makes no requests at all."""
    today = datetime.date.today()
    for r in old["rounds"]:
        day = datetime.date.fromisoformat(str(r["date"]))
        if day <= today and (not r["completed"] or (today - day).days <= QUIET_DAYS):
            return False
    return not (laps_on and laps_wanted(laps_dir, old["rounds"]))


def season_commit(season):
    """Latest F1DB commit that touched this season's folder (1 request), or None if unknown."""
    try:
        rows = json.loads(fetch(f"{API}/commits?path=src/data/seasons/{season}&per_page=1") or b"[]")
        return rows[0]["sha"] if rows else None
    except Exception as e:
        print(f"  F1DB: couldn't check for new commits ({e}), updating anyway")
        return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    season = int(args[0]) if args else datetime.date.today().year
    out = os.path.join(HERE, "data", str(season))
    jpath = os.path.join(out, "season.json")
    old = json.load(open(jpath, encoding="utf-8")) if os.path.exists(jpath) else None
    laps_on = os.environ.get("LAPS", "1") != "0"
    laps_dir = os.path.join(out, "laps")

    if old and not FORCE and nothing_due(old, laps_dir, laps_on):
        print(f"{season}: no race in the last {QUIET_DAYS} days and nothing missing, nothing to fetch (use --force to check anyway)")
        return
    state_path = os.path.join(out, "source_state.json")
    state = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {}
    sc = season_commit(season)
    if old and not FORCE and sc and sc == state.get("seasonCommit") and not (laps_on and laps_wanted(laps_dir, old["rounds"])):
        print(f"{season}: F1DB has nothing new for this season since the last run")
        return

    root, commit = sync_source(season)
    data = build(season, root, commit)
    if laps_on:
        update_laps(season, root, data)
    os.makedirs(out, exist_ok=True)
    if sc and sc != state.get("seasonCommit"):
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"seasonCommit": sc}, f)

    body = json.dumps({k: v for k, v in data.items()}, sort_keys=True)
    digest = hashlib.sha256(body.encode()).hexdigest()[:12]
    if old and old.get("contentHash") == digest:
        print(f"{season}: no changes (source {commit[:8]})")
        return
    data["contentHash"] = digest
    data["generatedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    changes = diff(old, data)
    new_rounds = sorted({r["round"] for r in data["driverRounds"]} - {r["round"] for r in (old or {}).get("driverRounds", [])})
    log_path = os.path.join(out, "changelog.json")
    log = json.load(open(log_path, encoding="utf-8")) if os.path.exists(log_path) else []
    log.append({"at": data["generatedAt"], "commit": commit, "newRounds": new_rounds, "corrections": changes})

    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=1)
    write_csv(os.path.join(out, "driver_points.csv"), data["driverRounds"])
    write_csv(os.path.join(out, "constructor_points.csv"), data["constructorRounds"])
    for name, rows in zip(("dash_drivers", "dash_constructors", "dash_rounds"), dashboard_rows(data)):
        with open(os.path.join(out, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, separators=(",", ":"))

    write_season_index()

    done = [r for r in data["rounds"] if r["completed"]]
    print(f"{season}: {len(done)}/{len(data['rounds'])} rounds, new rounds {new_rounds or 'none'}, "
          f"{len(changes)} corrections, {len(data['warnings'])} reconciliation warnings")
    for w in data["warnings"]:
        print("  warn:", w)


if __name__ == "__main__":
    main()
