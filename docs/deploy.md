# Updating the installed copy

The LaunchAgent on this Mac already runs the copy under Application Support. An update replaces that copy. It does not touch the archive the agent writes, and it does not load or unload the agent.

Two directories:

| Path | What it is |
| --- | --- |
| `~/Library/Application Support/isobar-data` | Code, `config/`, and the uv virtualenv. The plist runs this copy. |
| `~/Data/isobar` | The archive: `state.json`, `raw/`, `products/`, `manifest.json`, `status.json`, `attribution.json`, and `.lock`. |

`scripts/install-launchd.sh` deletes and recopies only `app/isobar_data` and `app/config` inside Application Support, then runs `uv sync --frozen` there and rewrites `~/Library/LaunchAgents/com.isobar.data.plist` in place. The program path stays `~/Library/Application Support/isobar-data/bin/isobar-data-launchd`. The script quotes that path, so the space in Application Support is safe. It never calls `launchctl`.

`~/Data/isobar` is not inside Application Support. Install does not read or delete it. Watermarks, token buckets, FTP checkpoints, and published files stay where they are.

## Steps

From the source tree you want installed, with `uv` on `PATH`:

```sh
scripts/install-launchd.sh
launchctl kickstart -k "gui/$(id -u)/com.isobar.data"
```

`kickstart -k` restarts the job that is already loaded so the next pass imports the new code. It is not `bootstrap` and not `bootout`. Do not unload the agent, and do not load it again.

The pass is short. If one is in the middle of writing when kickstart kills it, the data directory is still consistent: a new kite, aviation, or Bureau generation is a new directory, and `current.json` moves only after that directory is complete. The previous generation is left in place. The next start takes `.lock` and continues.

## Leave these alone

- Do not delete `~/Data/isobar`, and do not run `scripts/uninstall-launchd.sh` as an update. Uninstall removes the Application Support copy and the plist. It still does not delete the archive, but it is not how you update.
- Do not `launchctl bootout` or `launchctl bootstrap`. If kickstart says the service is not loaded, stop. The install script will not load it for you.
- Edits made only inside Application Support are overwritten. Change code or `config/` in the source tree, then run the two commands above.
