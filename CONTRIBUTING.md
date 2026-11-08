# Contributing to AirStation

Thanks for helping! Bug reports, docs fixes, theme packs and code are all welcome.

## Reporting problems

Open an issue with the template. Most problems are found fastest with the output of
`bash scripts/airctl status` plus the diagnostic command for the area
(`check-config`, `live-check`, `art-doctor`, `community.py status`). Remove passwords,
keys and anything private before posting. Security problems: see [SECURITY.md](SECURITY.md).

## Working on the code

- **Python** (`app/airstation`) uses the standard library only, with no pip
  dependencies, so the API image stays tiny. Keep it that way unless there's a strong
  reason.
- **Web** (`web/`) is plain HTML, CSS and JavaScript, with no build step.
- **Liquidsoap** (`config/radio.liq`) targets the version pinned in `.env.example`.
  Validate changes with `bash scripts/airctl check-config` on a real install.
- Run the tests before a pull request:

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
[docs/THEMING.md](docs/THEMING.md).

## Licence

By contributing you agree that your contribution is licensed under the
AGPL-3.0-or-later, like the rest of the project.
