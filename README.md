# FiveM FXServer Artifact Updater

A Python 3.13 command-line updater for **FiveM FXServer Linux artifacts**. It downloads the official recommended or latest build, validates and stages the archive, and replaces artifact-owned files while leaving top-level server configuration and resources in place.

> **Status:** This project has been syntax-checked, but has **not** been verified end-to-end against every official artifact or deployment layout. Test on a staging server and keep an independent backup before using it in production.

## Features

- Uses the official FiveM Linux server version API (no HTML scraping).
- Supports `recommended` (default) and `latest` channels.
- Skips installation when the recorded build is current, unless `--force` is supplied.
- Downloads and stages the artifact before modifying the installed server.
- Validates TAR paths and types, limits extracted size and entry count, and creates symlinks only after extracting files.
- Supports absolute symlinks inside the artifact's `alpine/` runtime without following them during extraction.
- Moves replaced top-level artifact entries to a timestamped backup directory using filesystem rename operations.
- Preserves top-level `server.cfg`, `resources`, and `txData` paths; does not back them up.
- Attempts rollback if an error occurs during file replacement.
- Optional cache cleanup with `--clear-cache`.
- Refuses to proceed when an FXServer process is detected for the current user.

## Requirements

- Linux (designed for Debian 13)
- Python **3.13**
- Python package: `requests`
- Sufficient free disk space for the downloaded archive, staging directory, and retained binary backups
- Write permission to the FXServer installation and backup directories

Install the dependency, preferably in a virtual environment:

```bash
python3.13 -m venv ~/fivem-updater-venv
~/fivem-updater-venv/bin/pip install requests
```

## Installation

Place the script in `/home/fivem/update.py` (or edit the paths in the script to match your environment). Stop FXServer and make a **separate, full backup** of your server configuration and resources before the first update.

```bash
cd /home/fivem
~/fivem-updater-venv/bin/python -m py_compile update.py
~/fivem-updater-venv/bin/python update.py
```

The script does **not** stop or restart FXServer automatically.

## Usage

```bash
# Recommended build (default)
python3.13 update.py

# Latest build
python3.13 update.py --channel latest

# Update and clear cache
python3.13 update.py --clear-cache

# Reinstall the selected build even if already recorded
python3.13 update.py --force

# Latest build, forced reinstall, clear cache
python3.13 update.py --channel latest --force --clear-cache
```

Use the virtual environment's Python executable instead of `python3.13` if you installed `requests` there.

## Default paths

| Purpose | Path |
| --- | --- |
| FXServer installation | `/home/fivem/FXServer/server` |
| Cache directory | `/home/fivem/fx-server/txData/QBCore/cache` |
| Artifact backups | `/home/fivem/artifact-backups` |
| Recorded installed build | `/home/fivem/FXServer/server/.artifact-version` |
| Installed artifact manifest | `/home/fivem/FXServer/server/.artifact-paths` |

Edit the `SERVER`, `CACHE`, and `BACKUPS` constants at the top of `update.py` if needed. **Verify the cache path:** the default cache path is different from the FXServer installation path.

## How updates work

1. Fetch build metadata from `https://changelogs-live.fivem.net/api/changelog/versions/linux/server`.
2. Download the selected official Linux `.tar.xz` artifact.
3. Validate and extract the archive into a temporary staging directory.
4. Check that FXServer is not running.
5. Move old artifact-owned top-level entries into a timestamped backup folder.
6. Move staged artifact entries into the installation directory.
7. Write `.artifact-version` and `.artifact-paths`.
8. Optionally remove the configured cache directory.

**Backups contain replaced artifact entries, not the complete server.** On the first run, without an existing `.artifact-paths` manifest, any existing top-level path matching an incoming artifact name is treated as artifact-owned and may be moved. Files *inside* a replaced artifact directory are not merged. Review your directory layout and take a full backup first.

## Safety and limitations

- **Stop FXServer first.** Process detection is a precaution, not a guarantee, especially if the server runs under another user or service manager.
- The installer uses multiple rename operations; the overall update is **not atomic**. An abrupt crash or power failure may require manual recovery.
- Rollback is attempted for errors during installation. It does **not** automatically revert a build that installs successfully but fails to start.
- Top-level protected paths are preserved, but custom files inside artifact-owned directories may be replaced.
- The script does not verify a cryptographic signature or pinned checksum for the artifact; it relies on HTTPS and the expected download host.
- Absolute symlinks in `alpine/` are retained as provided by the artifact. The updater avoids following them during extraction, but the installed runtime should still be treated as trusted executable software.
- `--clear-cache` removes the configured cache directory; double-check the path before enabling it.
- Old backup directories are **not** automatically pruned.

## Troubleshooting

**`FXServer appears to be running`** — Stop the server and confirm there are no relevant processes before retrying.

**`Artifact conflicts with protected names`** — The artifact contains a path reserved for configuration or updater metadata. Do not bypass the protection without inspecting the collision.

**`Unexpected artifact URL`** — The official API may have changed its URL format. Inspect the API response before updating the script.

**`Installed build: unknown`** — Normal on the first run when `.artifact-version` has not yet been created.

**`Permission denied`** — Ensure the account running the updater owns or can write the relevant server and backup paths. Avoid unnecessarily running the updater as root.

## Recovery

The script prints the backup directory after a successful replacement. If the new FXServer build does not start, **stop FXServer**, retain the backup, and inspect `.artifact-paths` and the backup contents before restoring. The tool currently has no dedicated `--rollback` command. Do not blindly copy a partial binary backup over an active installation.

## License

This project is licensed under the MIT License.
See the [LICENSE](LICENSE) file for details.

---

*Community-maintained utility; not affiliated with or endorsed by Cfx.re/FiveM.*
