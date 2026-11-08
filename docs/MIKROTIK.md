# Home link: MikroTik (RouterOS 7) WireGuard + SMB

## How this setup works

This guide covers the reference setup: the music library is a USB drive plugged into a
MikroTik router and shared by the router's own SMB server. Any SMB server reachable over
WireGuard works (see Option B for a home Linux device). It assumes **RouterOS v7**.

In the reference setup, the router is both the VPN endpoint and the file server:

```
VPS 10.77.88.1  ==WireGuard (router dials out)==>  MikroTik 10.77.88.2  ->  USB disk (SMB)
```

The VPS mounts `//10.77.88.2/YourShare` directly. There's no LAN forwarding, no NAT and
no routes to your home network, and the VPS can reach only the router's SMB port. The
router always dials out to the VPS, so this works behind CGNAT and needs no port
forwarding at home.

---

## Step 1: Check the router

In Winbox > New Terminal:

```
/system resource print
/system package print
```

Note `version`, `architecture-name` and `board-name`.

- WireGuard exists from RouterOS **7.1**. The commands below avoid newer-only options
  (no `name=` on peers, no `responder=`, no `client-*` fields), so any 7.x release is fine.
- The current RouterOS SMB server supports SMB 2.1, 3.0 and 3.1.1. Older 7.x releases
  had a more limited SMB server. **If the SMB test in step 5 fails on every dialect,
  update RouterOS first.** Take a backup (`/system backup save name=pre-upgrade`, then
  download it from Files), then `/system package update check-for-updates` and
  `/system package update install` on the **stable** channel. Then upgrade the
  RouterBOOT firmware (`/system routerboard upgrade`, reboot).
- MikroTik doesn't support the SMB server on **SMIPS** devices (hAP lite, hAP mini and
  similar). If yours is one, use Option B below with the USB disk moved to the Linux
  device.

## Step 2: Generate the commands on the VPS

```bash
cd /opt/air-station
bash scripts/wg-setup.sh keys
```

You get a block like this (saved as `/root/air-mikrotik.rsc`), with your values filled in:

```
/interface wireguard add name=wg-air listen-port=13232 mtu=1420 comment="AirStation VPS link"
/ip address add address=10.77.88.2/30 interface=wg-air comment="AirStation VPS link"
/interface wireguard peers add interface=wg-air public-key="…VPS key…=" endpoint-address=203.0.113.10 endpoint-port=51820 allowed-address=10.77.88.1/32 persistent-keepalive=25s comment="AirStation VPS"
:if (…) do={ /ip firewall filter add chain=input in-interface=wg-air src-address=10.77.88.1 protocol=tcp dst-port=445 action=accept comment="AirStation: VPS reads SMB share" place-before=… }
:if (…) do={ /ip firewall filter add chain=input in-interface=wg-air src-address=10.77.88.1 protocol=icmp action=accept … }
/interface wireguard print detail where name=wg-air
```

What each line does:

1. Creates a **separate** WireGuard interface `wg-air` on port 13232, so an existing
   `wireguard1` (e.g. a phone VPN on 13231) is untouched.
2. Gives the router `10.77.88.2/30` on that interface.
3. Adds the VPS as the only peer. `allowed-address=10.77.88.1/32` means the only
   traffic that can come in over this tunnel is from the VPS, and nothing of yours is
   routed out through it.
4. and 5. Firewall: accept SMB (445) and ping from the VPS on `wg-air`, placed **at the
   top** of the input chain so the default "drop all not from LAN" rule can't block it.
6. Shows the router's own public key.

If `10.77.88.0/30` clashes with anything you already use, rerun with another range:
`WG_NET=10.99.1 bash scripts/wg-setup.sh keys`, and use the same `WG_NET=…` in step 3.

## Step 3: Paste into the router, then finish on the VPS

Paste the lines **one at a time** into Winbox > New Terminal. Each should return
silently. The last one prints something like:

```
 0  R name="wg-air" mtu=1420 listen-port=13232 private-key="…" public-key="AbC…xyz="
```

Copy the `public-key` value (**not** the private key), then on the VPS:

```bash
bash scripts/wg-setup.sh peer 'AbC…xyz='
```

**Check:** it shows `Handshake OK` and a ping reply from `10.77.88.2`. On the router,
`/interface wireguard peers print detail` shows a recent `last-handshake`.

## Step 4: SMB on the router

```
/ip smb print
/ip smb shares print
/ip smb users print
```

1. **Enabled:** `/ip smb set enabled=yes` if it isn't already.
2. **Interfaces:** if `interfaces` (or `interface`) isn't `all`, add `wg-air` to the
   existing list. For example, if it currently says `bridge`:
   `/ip smb set interface=bridge,wg-air`. Keep whatever is already there, or your PCs
   lose access.
3. **Read-only user for the VPS:**
   `/ip smb users add name=airsync password=A-LONG-RANDOM-PASSWORD read-only=yes`
4. **Share access:** if your share has `valid-users` set, add `airsync`, e.g.
   `/ip smb shares set [find name="YourShare"] valid-users=youruser,airsync`.

Nothing here opens SMB to the Internet: the default MikroTik firewall drops WAN input,
and the rule you added only accepts 445 from `10.77.88.1` on `wg-air`.

## Step 5: Test the share from the VPS

```bash
bash scripts/wg-setup.sh status          # handshake, ping, and port 445 check
bash scripts/wg-doctor.sh                # if there's no handshake: finds which side is wrong
bash scripts/nas-setup.sh test 10.77.88.2 'YourShare'
```

Use `airsync` and its password when asked. Then continue with **DEPLOY.md step 8**.

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `expected end of command` or `no such argument` when pasting | An option this RouterOS build doesn't know. Check you pasted the whole line (Winbox can wrap it). The generated commands avoid 7.15+-only options, so if it still fails, contact support (SUPPORT.md) with the exact line and the error. |
| `failure: … already have interface with such name` | You ran step 2 twice. Check with `/interface wireguard print`. To start over: `/interface wireguard remove [find name=wg-air]`. |
| No handshake, and you added options copied from your phone peers | Phone ("road warrior") peers usually have **`responder=yes`**, which means *never dial out*. This link needs the router to dial the VPS, so remove it: `/interface wireguard peers set [find interface=wg-air] responder=no`. If you set a **`private-key`** on the peer (or generated a client config/QR for it), RouterOS replaces the peer's public key with one derived from that private key, so it no longer matches the VPS. Remove the peer and paste the `peers add` line from `/root/air-mikrotik.rsc` again. |
| The peer's "client config" / QR code connects on a phone but can't ping 10.77.88.2 | That config turns the phone into a copy of the *VPS* peer, so it isn't a test of this link. It dials the router's own port (13232), which the default MikroTik firewall drops from the Internet, so the phone app shows "connected" without ever handshaking. Delete that tunnel from the phone and use `bash scripts/wg-doctor.sh` on the VPS instead. |
| Router `tx` grows but `rx=0`, and wg-doctor shows packets arriving with no reply | The VPS host firewall drops them. On a VPS with **ufw** active: `ufw allow 51820/udp`. Docker-published ports (80/443/8005…) bypass ufw, which is why the site worked while WireGuard didn't. `wg-setup.sh peer` adds this rule automatically. |
| No handshake at all | 1) Your VPS provider's firewall must allow **UDP 51820** inbound. 2) On the VPS, `ss -ulpn \| grep 51820` must show WireGuard listening. 3) Keys swapped: the router's peer must hold the **VPS** key, and `wg-setup.sh peer` needs the **router** key. 4) `endpoint-address` must be the VPS public IP. 5) Router clock roughly correct (`/system clock print`). |
| Handshake OK, but ping fails | The firewall rule landed below a drop rule. `/ip firewall filter print` and drag "AirStation" rules to the top in Winbox. Also check the address: `/ip address print where interface=wg-air` should be `10.77.88.2/30`. |
| Ping OK, port 445 not reachable | SMB not enabled or not listening on `wg-air` (step 4.2). |
| `mount error(13): Permission denied` | Wrong SMB user/password, or the user isn't in the share's `valid-users`. Fix and rerun; delete `/root/.air-smbcredentials` to re-enter. |
| `mount error(2): No such file or directory` | Share name wrong. It's the **share** name from `/ip smb shares print`, not the folder path on the disk, and case matters. |
| `mount error(95)` / `Operation not supported` on every version | The router's SMB server is older than SMB 2.1: update RouterOS (step 1). |
| Small files copy, big ones stall | MTU. Set `mtu=1380` on the router (`/interface wireguard set wg-air mtu=1380`) and `MTU = 1380` in `/etc/wireguard/wg-air.conf`, then `systemctl restart wg-quick@wg-air`. |
| Works, then stops after some hours | Check `persistent-keepalive=25s` is on the router's peer. If your home IP changes that's fine: home dials out. |

**About LAN addressing:** some home networks use addresses like `172.201.201.1` for the
router. The `172.201.x.x` range is **public Internet address space**, not private (the
private range is 172.16.0.0–172.31.255.255). It works at home, but any real Internet host in that range
becomes unreachable from your LAN. It doesn't affect this tunnel. If you ever re-address
the LAN, choose something like `192.168.77.0/24`.

---

## Option B: WireGuard on a home Linux device

Use this if the router route doesn't work. An always-on device (e.g. a Raspberry Pi or
mini PC) dials the VPS and forwards
**only** SMB (TCP 445) to the router's LAN address. The router needs no changes except
an SMB user, and it sees the device as a normal LAN client.

1. On the device: follow the header of `config/wireguard/home-linux-peer.example.conf`
   (install `wireguard-tools nftables`, generate a key, fill in `VPS_PUBLIC_KEY`,
   `VPS_PUBLIC_IP` and `ROUTER_LAN_IP`, then `systemctl enable --now wg-quick@wg-air`).
   Get the VPS key with `bash scripts/wg-setup.sh keys` (ignore the RouterOS block).
2. On the VPS: `bash scripts/wg-setup.sh peer 'DEVICE_PUBLIC_KEY' ROUTER_LAN_IP`
3. Test and mount against the **router's LAN IP**:
   `bash scripts/nas-setup.sh test ROUTER_LAN_IP 'YourShare'`

The device must stay on. If it's off, the station plays from its cache just like any
other home outage.

## Removing the link

Router:

```
/ip firewall filter remove [find comment~"AirStation"]
/ip address remove [find interface=wg-air]
/interface wireguard peers remove [find interface=wg-air]
/interface wireguard remove [find name=wg-air]
```

VPS: `systemctl disable --now wg-quick@wg-air air-sync.timer`, and remove the AirStation line
from `/etc/fstab`.
