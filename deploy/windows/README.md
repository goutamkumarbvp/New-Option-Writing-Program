# IORT on a Windows PC

Everything your Windows 10/11 PC needs to run the terminal with Kotak Neo live data. Order routing stays **LOCKED** (`LIVE_TRADING=false`): the terminal shows live data and refuses every order until you pass the controlled live test.

## 1. What the PC needs

| Need | Why | How |
|---|---|---|
| Windows 10 (2004 or newer) or Windows 11, 64-bit, 8 GB RAM | Docker Desktop runs Linux containers in WSL 2 | `winver` shows the version |
| Virtualization on in the BIOS | WSL 2 needs it | Task Manager > Performance > CPU > "Virtualization: Enabled" |
| Docker Desktop | runs the terminal, Postgres and Redis | the installer offers `winget install Docker.DockerDesktop`, or download it from docker.com |
| Git for Windows | to get and update the code | `winget install Git.Git` |
| A wired or stable internet line | the Kotak feed reconnects by itself, but every drop blanks the screen | |
| Correct clock | TOTP codes depend on it | Settings > Time & language > Date & time > "Set time automatically" on, then **Sync now** |
| Outbound HTTPS (443) to `*.kotaksecurities.com` | login, scrip master, live feed (`wss://sfeed.kotaksecurities.com`) | allow it in any firewall, antivirus or office proxy |
| A static public IP registered with Kotak | **only for orders**; live data works without it | ask your ISP for a static IP, register it in Kotak Neo > Trade API, then set `REGISTERED_STATIC_IP` |

## 2. Get the code

```powershell
git clone https://github.com/goutamkumarbvp/New-Option-Writing-Program.git C:\IORT
cd C:\IORT
```

## 3. Prepare Kotak Neo

1. In Kotak Neo, open **Trade API** and create (or regenerate) the app. Copy the **consumer key**.
2. Register **TOTP** for the Trade API. When the QR code is shown, also copy the **secret** (the text behind the QR code, letters A-Z and digits 2-7). Add it to your authenticator app as usual.
3. Keep your **MPIN**, **UCC** (client code) and registered **mobile number** ready.

> If these values were ever pasted into a chat, an email or a file someone else can read, regenerate the consumer key and the TOTP secret first. Never paste them anywhere except the installer prompt or your own `.env`.

## 4. Install (once)

Open **PowerShell** (no administrator needed, except for the clock resync step) and run:

```powershell
cd C:\IORT
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -KeepAwake
```

The installer:

- checks Windows, WSL 2 and Docker Desktop;
- creates `.env` from `.env.example` and generates the database password, operator token and audit key;
- asks for your Kotak login (MPIN and TOTP secret are typed hidden), checks the format, writes it to **section 1 of `.env`** and makes the file readable only by your Windows user;
- resyncs the clock and, with `-KeepAwake`, stops the PC sleeping on AC power (a sleeping PC has no feed);
- builds and starts the terminal, runs the diagnostics and opens <http://localhost:8000>.

To edit the login later, open `.env` in Notepad: section 1 is the **KOTAK NEO LOGIN** block. Save as UTF-8, then run `deploy\windows\start.ps1`.

## 5. Daily use

| Task | Command |
|---|---|
| Start (also starts Docker Desktop if needed) | `deploy\windows\start.ps1` |
| Stop (data is kept) | `deploy\windows\stop.ps1` |
| Diagnostics | `deploy\windows\doctor.ps1` (add `-Login` for one real Kotak login test) |
| Logs | `deploy\windows\logs.ps1` |
| Back up the database | `deploy\windows\backup.ps1` (saved under `backups\`) |
| Update to the latest code | `deploy\windows\update.ps1` (backs up first; `.env` is never touched) |
| Start automatically at logon | `deploy\windows\autostart.ps1` (remove with `-Remove`) |

The operator token for control buttons (Reconnect now, kill switch reset, cancel) is `OPERATOR_API_TOKEN` in `.env`. Paste it into the Execution tab once per browser tab.

## 6. What repairs itself

- **Kotak login**: logs in before the open (08:50 IST), replaces a session from an earlier day, and when Kotak ends a session it logs in again and repeats the call once. A Kotak maintenance page, a 5xx or a network error backs off for 5 to 60 s and never counts as a wrong password. Two real refusals in a row halt automatic login, so the account cannot be locked: fix `.env`, then press **Reset KOTAK login** on the Overview tab.
- **Clock drift**: the terminal measures its clock against Kotak's and computes TOTP codes on Kotak's time. Docker Desktop's Linux VM keeps its own clock and can drift after the PC sleeps. If the diagnostics report a drift, run `wsl --shutdown` and restart Docker Desktop.
- **Live feed**: a dropped Kotak feed reconnects at once, then after 1, 2, 5, 10 and 20 s (cap 30 s). The Market tab shows its state, with **Reconnect now** and an **Auto** on/off switch.
- **Background loops**: every loop (order monitor, risk monitor, reconciliation, loaders) restarts by itself if it crashes. The System tab shows each loop and its restarts.
- **Redis and Postgres**: reconnect by themselves. While the database is down a kill switch still holds in memory and is written as soon as the database returns.
- **Browser**: the page reconnects its event stream by itself and blanks every number when the backend stops answering.

## 7. Live data only

The terminal never shows simulated or sample data. Without a live Kotak feed every price, chain, P&L and chart is blank ("No live data"), and a quote older than `DATA_STALE_MS` is blanked rather than shown as current.

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Overview shows `NOT_LOGGED_IN` or `AUTH_ERROR` | Run `doctor.ps1`. It names the wrong setting (mobile must be `+91` and 10 digits, MPIN 6 digits, TOTP secret base32). |
| `LOGIN_HALTED` | Kotak refused the login twice. Fix the value in `.env`, `stop.ps1` then `start.ps1`, then **Reset KOTAK login** on the Overview tab. |
| `KOTAK_TOTP_SECRET_INVALID` | You pasted the 6-digit code instead of the secret. Use the base32 text behind the QR code. |
| Diagnostics: Kotak hosts FAIL | A firewall, antivirus or proxy blocks `*.kotaksecurities.com`. Allow outbound HTTPS 443. |
| Diagnostics: clock FAIL | Settings > Time > Sync now; then `wsl --shutdown` and restart Docker Desktop. |
| Chain shows "No live data" during market hours | Check the Market tab feed state; press **Reconnect now**. Check that the instrument master loaded today (System tab). |
| Orders refused with `STATIC_IP_NOT_CONFIGURED` / `PUBLIC_IP_NOT_REGISTERED` | Only with live order routing: register this PC's static public IP with Kotak and set `REGISTERED_STATIC_IP`. |
| `database password authentication failed` | `.env` has a different `POSTGRES_PASSWORD` than the existing database. Put the old one back. `docker compose down -v` deletes all data, so avoid it. |
| Old Kotak values keep coming back | Remove old Windows environment variables: `[Environment]::SetEnvironmentVariable('KOTAK_MPIN', $null, 'User')` (repeat for each `KOTAK_*`), then open a new PowerShell. The terminal reads the login from `.env` only. |

These scripts were syntax-checked with PowerShell 7 and their `.env` handling was exercised on Linux; they have not yet been run on a real Windows PC. Report any step that fails with the text it prints.
