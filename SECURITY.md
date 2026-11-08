# Security

Please report security problems privately to **[SECURITY E-MAIL]**, with steps to
reproduce and the affected version (Studio > Licence shows it). Don't post them in
public forums or chats.

Things that are intentional and not vulnerabilities:
- Custom JavaScript from Admin > Appearance runs on the public pages. Only admins can
  set it, and it is never loaded in the admin itself.
- The admin password is the only admin account. Protect it, and keep `secrets/`
  readable by root only (`init.py` sets this).
