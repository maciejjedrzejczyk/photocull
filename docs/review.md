# Review gallery (`./review`)

Reading a CSV is fine for triage, but eyeballing the actual photos is faster.
`review` starts a small local web app that turns the report into an interactive
gallery:

```bash
./review photo_quality_report.csv                       # opens your browser
./review report.csv --port 8765 --quarantine ~/_rejects --root ~/Pictures
```

In the browser you work a **grid-first triage** flow:

- **Worst-first grid (default).** The grid opens sorted by tier, worst first.
  Filter by **tier** (delete / duplicate / review / keep) and by **signal**
  (Blurry / Too dark / Noisy / …) using the chips, search by filename, and sort
  by any metric or by **cluster**.
- **Mark, don't delete (yet).** Tick a card (or use the keyboard in focus mode)
  to mark it **✗ for deletion**; shift-click ticks a whole range. Marking is
  reversible and moves nothing.
- **Commit in a batch.** When you're happy, **Commit ✗ → Trash** (recoverable)
  or **Commit ✗ → Quarantine** acts on everything marked at once. An **Undo
  commit** button restores the last batch to its original location.
- **Compare clusters.** Sort by **cluster** to see each near-duplicate group
  together with its keeper highlighted; **Mark others ✗** rejects every frame in
  the group except the keeper in one click.
- **Focus mode (optional, keyboard-first).** Click a thumbnail or press
  **Focus mode** for a full-screen, one-photo-at-a-time view with the reason and
  metrics shown. Keys: `←`/`→` move, `X`/`⌫` reject &amp; next, `K`/`↵` keep &amp;
  next, `U` unmark, `Z` zoom (check focus at full size), `B` brighten (judge
  dark shots), `Esc` back to the grid.
- **Tune thumbnails live** with the *size* and *quality* dropdowns; your choice
  is remembered per browser.

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
  permanent-delete path in this tool. **Undo commit** moves the last committed
  batch back from the Trash/quarantine to its original location.

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
