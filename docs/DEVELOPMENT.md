# Developing AirStation

Notes for working on AirStation's code (the seller's development copy). Buyers may
change the code for their own station under the terms of [LICENSE](../LICENSE).

## Reporting problems

Contact support (see [SUPPORT.md](../SUPPORT.md)). Most problems are found fastest with the output of
`bash scripts/airctl status` plus the diagnostic command for the area
(`check-config`, `live-check`, `art-doctor`, `community.py status`). Remove passwords,
keys and anything private before posting. Security problems: see [SECURITY.md](../SECURITY.md).

## Working on the code

- **Python** (`app/airstation`) uses the standard library only, with no pip
  dependencies, so the API image stays tiny. Keep it that way unless there's a strong
  reason.
- **Web** (`web/`) is plain HTML, CSS and JavaScript, with no build step.
- **Liquidsoap** (`config/radio.liq`) targets the version pinned in `.env.example`.
  Validate changes with `bash scripts/airctl check-config` on a real install.
- Run the tests before a release:

  ```bash
  python3 -m unittest discover -s tests -v     # needs ffmpeg
  ```

- Preview the site without Docker: start the API with the fake Liquidsoap from the
  tests (see `tests/fake_liquidsoap.py`), then `python3 tests/devserver.py` and open
  http://localhost:8090.
- Keep changes focused, add or update a test for behaviour changes, and note
  user-visible changes in `CHANGELOG.md` (and `docs/UPDATING.md` if an upgrade needs a
  step).

## Theme packs

Made a look you're proud of? Export it with `bash scripts/airctl branding export` and
share the zip (only with images you have the rights to). The format is in
[THEMING.md](THEMING.md).

## Releases and licensing

The repository is a **developer build**: `PUBLIC_KEY` in `app/airstation/licensing.py`
is empty, so there is no trial and no lock. Sellable zips are built with
`scripts/dev/release.py`, which writes your licence server's public key and address
into a copy (see `docs/LICENSING.md`, "For the seller"). Never commit a release copy.
