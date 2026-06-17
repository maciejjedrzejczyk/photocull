# Review gallery (`./review`)

Reading a CSV is fine for triage, but eyeballing the actual photos is faster.
`review` starts a small local web app that turns the report into an interactive
gallery:

```bash
./review photo_quality_report.csv                       # opens your browser
./review report.csv --port 8765 --quarantine ~/_rejects --root ~/Pictures
```

In the browser you can:

- **Filter by tier** (delete / duplicate / review / keep) and sort by any
  metric — or by `cluster`, which groups near-duplicates with the keeper marked.
- **Click any thumbnail** for a full-size view with all the metrics, and arrow
  through the set.
- **Select** photos individually or a whole page, see the total size selected,
  and **bulk-move them to the macOS Trash** (recoverable) or to a quarantine
  folder.

HEIC/HEIF are transcoded to JPEG on the fly so they display in any browser.
Photos are read straight from their original locations — nothing is copied.

## Options

| Option              | Description                                              |
|---------------------|----------------------------------------------------------|
| `csv`               | Path to the photocull CSV (default `photo_quality_report.csv`). |
| `--port`            | Port to serve on (default `8765`).                       |
| `--host`            | Bind address (default `127.0.0.1`; keep it local).       |
| `--quarantine DIR`  | Enable the Quarantine action, moving files into `DIR`.   |
| `--root DIR`        | Library root used to preserve structure on quarantine (repeatable). |
| `--no-browser`      | Don't auto-open the browser.                             |

## Safety and security

The server can move files, so it is locked down:

- It **binds to `127.0.0.1` only** — never exposed to your network.
- It will *only* serve or delete files that appear in the CSV (**path
  whitelist**); crafted URLs cannot touch anything else on disk.
- Destructive actions require a **per-session token** and a localhost `Host`
  header, so other browser tabs or websites can't drive deletions.
- "Delete" moves files to the **macOS Trash** (recoverable) — there is no
  permanent-delete path in this tool.
