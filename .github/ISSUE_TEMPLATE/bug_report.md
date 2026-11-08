---
name: Bug report
about: Something doesn't work as described in the docs
labels: bug
---

**What happened, and what did you expect?**

**Steps to reproduce**

**Version and setup**
- AirStation version (`cat app/airstation/__init__.py`):
- OS / VPS size:
- Community add-on on? (yes/no)

**Output** (remove passwords, keys and domains you don't want public)
```
bash scripts/airctl status
# and, depending on the problem:
bash scripts/airctl check-config
bash scripts/airctl live-check
bash scripts/airctl art-doctor
python3 scripts/community.py status
```
