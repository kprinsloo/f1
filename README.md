# F1 Points Tracker

Driver and constructor points after every Grand Prix, rebuilt from [F1DB](https://github.com/f1db/f1db),
plus lap-by-lap data from [OpenF1](https://openf1.org), shown as a web page:
**https://kprinsloo.github.io/f1/**

## How it runs

- `.github/workflows/update.yml` runs at 06:47 UTC on Monday, Tuesday and Wednesday, March to December only.
  It runs `update.py`, commits any new results into `data/<season>/`, and republishes the page only when
  the data changed.
- `update.py` keeps requests low: it makes no request at all between race weekends (no race in the last
  10 days), one request when F1DB has nothing new, and stops after 400 requests in any run.
- To run it by hand: **Actions** tab → **Update F1 tracker** → **Run workflow**. Tick *force* to check F1DB
  even if no race was held recently, or type a season (e.g. `2026`) to rebuild an older year.

## One-time settings

1. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
2. Run the workflow once from the **Actions** tab to publish the page.

## On your own PC

Double-click `run.bat`. It updates the data, then serves the dashboard at http://localhost:8000/
(opening `index.html` straight from disk shows a blank page). Only standard Python is needed.
`python update.py --force` checks for new data even between race weekends.

## Good to know

- GitHub pauses scheduled workflows in a repo with no commits for 60 days, which can happen over the winter
  break. If that happens, open **Actions → Update F1 tracker → Enable workflow** before the first race.
