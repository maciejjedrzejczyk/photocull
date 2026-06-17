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
- **Adjust thumbnail resolution and quality live** with the *size* and
  *quality* dropdowns in the header — thumbnails re-fetch immediately and your
  choice is remembered (per browser) across sessions.

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
| `--thumb-size PX`   | Default thumbnail resolution, longest edge (160-2000, default 640). Adjustable live in the UI. |
| `--thumb-quality Q` | Default thumbnail JPEG quality, 0.3-1.0 (default 0.85). Adjustable live in the UI. |
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

## Reaching it from another machine

By default the server binds to `127.0.0.1`, so it's only reachable on the Mac
itself. There are two ways to view it remotely:

**Recommended — SSH tunnel (no exposure).** Keep the default localhost bind and
forward the port over SSH from the remote machine:

```bash
ssh -L 8765:127.0.0.1:8765 you@your-mac
# then open http://127.0.0.1:8765 on the remote machine
```

The traffic is encrypted and the server is never exposed to the network.

**Direct bind (not recommended).** You can bind to a network address:

```bash
./review report.csv --host 192.168.0.122      # reachable at that IP
./review report.csv --host 0.0.0.0            # reachable on all interfaces
```

The server then accepts requests whose `Host` matches that address (a wildcard
bind accepts any Host). **Be aware:** the server is *unauthenticated* — the
session token is handed to whoever loads the page — so anyone who can reach the
IP:port can view your photos and move them to the Trash/quarantine. The tool
prints a security warning when you do this. Only use it on a network you fully
trust, and prefer the SSH tunnel.
