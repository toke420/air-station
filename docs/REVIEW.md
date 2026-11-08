# Review of the v1 "foundation" bundle

What was checked: every file in `air-station-foundation.zip` (README, compose,
Dockerfile, Liquidsoap and Icecast config, scripts, systemd units, tests, docs).

## What was good, and kept

- **The radio never reads SMB directly.** A separate host job copies music locally,
  so a hung NAS can't freeze audio. Kept, and improved.
- Non-root containers, dropped capabilities, read-only filesystems, log caps, and
  secrets in files rather than environment variables. Kept.
- Atomic file writes, verifying copies with ffprobe, the disk-space reserve, and never
  deleting recordings automatically. Kept.
- Rendering Icecast passwords from secrets with proper XML escaping. Kept, and it now
  also sets the hostname.

## Why you got stuck

1. **The WireGuard section had no router commands.** It said "this bundle deliberately
   does not install guessed RouterOS interfaces, routes or firewall rules" and pointed
   at a Linux template that "is not RouterOS syntax". That left you to translate it
   through trial and error. v2 generates the exact RouterOS 7 commands with your VPS key
   and IP filled in (`scripts/wg-setup.sh keys`) and gives a troubleshooting table.
2. **It assumed a separate NAS behind the router.** It talked about LAN forwarding, return
   routes and SNAT. Your share is served by the MikroTik itself, so none of that is
   needed: the VPS mounts the router's own tunnel address.
3. **SMB 3.0 was hard-coded** (`vers=3.0`) with no way to discover what the router
   supports. v2's `nas-setup.sh` tries 3.1.1, 3.0, 2.1 and 2.0 and writes the one that works.
4. **No automation for error-prone steps.** Hand-editing `fstab` and the WireGuard
   config, and pasting keys. Each is now a script with checks and backups.

## Design gaps fixed in v2

| v1 | Problem | v2 |
|---|---|---|
| Random 1 GB batch every 6 h, shuffled | No scheduling, no requests, no fair rotation, repeats across batches; the opposite of "better playlist management" | API-driven AutoDJ: folder/genre/hand-picked playlists, weekly show schedule, requests, whole-library fair shuffle, admin UI |
| `reload_mode="watch"` on a playlist that's replaced by atomic rename | inotify watches the old file, so reloads can be missed | Liquidsoap asks the API per track; the emergency folder is rescanned every 5 min |
| A silent but connected DJ stays on air | Dead air if Mixxx is left connected | `blank.strip`: 15 s of silence hands back to AutoDJ |
| After a live set, AutoDJ resumes mid-song | Sounds broken | Interrupted track is skipped; a fresh song starts |
| Everything bound to 127.0.0.1; Mixxx over an SSH tunnel | Not usable as a public station | Caddy HTTPS for the site and streams; Mixxx on :8005; OBS via SRT/RTMP |
| No website, API, now-playing or history | Mockup not started | Branded site from your mockup, now playing with art, history, schedule, requests, community links |
| OBS/MediaMTX "later" | — | MediaMTX → ffmpeg → Liquidsoap `/obs`, with priority below Mixxx |
| Liquidsoap 2.3.2 from Debian | Older branch; features depend on how Debian built it | Official `savonet/liquidsoap:v2.4.5` image |
| Manual "record everything" container | Records AutoDJ too; easy to forget | Automatic per-set recording of live shows (or everything), with the disk guard kept |
| One 192 kbps stream | Heavy on mobile data | Adds a 96 kbps "data saver" mount |
| NAS mounted as root-only, job ran as root | Broader privileges than needed | Mount readable by the `air` user only; job runs unprivileged |
| Stale-cache detection by checking file ages | Hard to see | Dashboard shows NAS reachable/unreachable, last scan, cache fill and queue readiness |

## Not verified in my environment

I ran the Python code (22 automated tests: tag reading, schedule edge cases, fair
shuffle, requests, NAS index/copy/evict, the full HTTP API with a simulated Liquidsoap)
and rendered the website and admin in a real browser. `docker compose config` validates.

I couldn't pull Docker Hub images here, so **`config/radio.liq` hasn't been executed by
a real Liquidsoap**. It's written against the Liquidsoap 2.4 documentation, and
DEPLOY step 6 runs Liquidsoap's own checker (`airctl check-config`) before anything
starts. If that reports an error, send me the output and I'll fix it.
