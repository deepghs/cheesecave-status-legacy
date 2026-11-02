# CheeseCave status page

A public status page for one [CheeseCave](https://github.com/cheesecave) instance: a green, yellow and red
bar per service for the last 90 days, the current state, and the past incidents. It is a template: nothing in
the code names an instance, everything about the instance is in [`status.config.json`](status.config.json).

Look at it: <https://deepghs.org/cheesecave-status/> (the instance of this repository: DeepGHS Hub).

## How it works

```
Monitor workflow  --every 5 min-->  checks the instance  -->  `status` branch  (checks/*.jsonl, status.json)
                                                                     ^
Pages (static page, deployed rarely)  --the page reads status.json every minute----+
```

- **Monitor** (`scripts/monitor.py`, standard library only) probes every component of the config, appends the
  result to `checks/YYYY-MM-DD.jsonl`, rebuilds `status.json`, and force-pushes one fresh commit to the
  `status` branch (the files are the history, so the branch does not grow). Outages open and close GitHub issues
  labelled `status` after two failed rounds in a row, so watchers of the repository get a notification.
- **Pages** (`site/index.html`) is a static page. It reads `status.json` from the `status` branch every minute
  and falls back to the copy deployed with the page. It is deployed only when the page or the config changes,
  so a new check never needs a deployment (GitHub limits branch-based Pages builds to 10 per hour).
- The page shows "Status data is out of date" when the last check is older than three intervals, so a stopped
  monitor is never shown as green.
- Why a long job and not `schedule`: GitHub's cron is best-effort; here it fired about every 3 hours. One run of
  Monitor loops for about 5 h 35 min and starts the next copy of itself (`workflow_dispatch` is allowed with the
  default token); the hourly cron only restarts the chain if it broke. The checks come from GitHub's servers
  (one region), against public pages only.

## Use it for another instance

1. Create a public repository from this one (fork or "use this template") **in an account that is not served by
   the instance itself**, so that the status page survives an outage of the instance.
2. Edit [`status.config.json`](status.config.json):

   | key | meaning |
   |---|---|
   | `name`, `base_url` | shown in the header; the address that `path` is relative to |
   | `components[].name` | shown on the page; unique |
   | `components[].path` or `.url` | exactly one of them |
   | `components[].expect_status` | accepted status codes (default: any 2xx; redirects are followed) |
   | `components[].expect_json` | e.g. `{"api": "cheesecave"}`: keys the JSON body must have, to catch an error page served with `200` |
   | `components[].headers`, `.max_bytes` | e.g. a `Range` header; how much of the body to read |
   | `interval_seconds`, `timeout_seconds`, `degraded_ms`, `history_days`, `confirm_failures` | defaults: 300, 20, 8000, 90, 2 |

   A useful set is the home page, `/api/version` (answers `{"api": "cheesecave", ...}`), the listing of a public
   repository and a `Range` request for a small file of it (that follows the redirect to the object storage).
3. Settings: Pages, source **GitHub Actions**; Actions, workflow permissions **read and write**.
4. Run the **Monitor** workflow once (Actions tab); it keeps itself going. Run **Pages** once to deploy the page.

## Develop

```sh
python3 -I scripts/test_statuslib.py     # unit tests, no network
python3 scripts/monitor.py --minutes 1 --remote /path/to/a/bare/repo   # a local round trip
python3 scripts/build_pages.py --out public --repo OWNER/REPO          # then serve `public/`
```

The repository was started from the [Upptime](https://github.com/upptime/upptime) template (MIT, see
[`LICENSE`](LICENSE)); none of its code is used any more.
