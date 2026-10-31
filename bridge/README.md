# Herdr Remote bridge

Run this on the computer where herdr runs to control its agents from the Herdr Remote iPhone app. Requires Python 3.10+, `hc` and `herdr-app`.

## Quick start

Install herdr first, with `hc` and `herdr-app` available on PATH. Install Tailscale on
this computer and your phone, sign into the same tailnet, and connect both devices.
Git is required for pipx's git install.

### macOS

```sh
curl -fsSL https://raw.githubusercontent.com/WesMedia8/herdr-remote/main/bridge/install/install.sh | sh
```

Requires Python 3.10+ (`brew install python`). The installer installs pipx with
Homebrew if needed, pairs the phone, and offers login startup.

### Ubuntu/Linux

```sh
curl -fsSL https://raw.githubusercontent.com/WesMedia8/herdr-remote/main/bridge/install/install.sh | sh
```

If prerequisites are missing, run `sudo apt install python3 python3-venv pipx git`
yourself, then rerun. The installer never runs sudo. Login startup requires a
working systemd user session. For startup without login: `loginctl enable-linger "$USER"`
(may require authorization).

### Windows (PowerShell 5+)

```powershell
irm https://raw.githubusercontent.com/WesMedia8/herdr-remote/main/bridge/install/install.ps1 | iex
```

Install Python if needed with `winget install Python.Python.3.12` (include `py`),
and Git with `winget install Git.Git`, then reopen PowerShell. The installer installs
pipx, pairs, and registers a hidden task at logon for the current user. Tailscale:
`winget install Tailscale.Tailscale`, then open the app and sign in.

### Manual pipx route

```sh
pipx install 'git+https://github.com/WesMedia8/herdr-remote#subdirectory=bridge'
herdr-remote-bridge pair
herdr-remote-bridge serve
# Optional login startup:
herdr-remote-bridge service install
```

Installers use `pipx install --force` to refresh an existing installation.
Scan the terminal QR in the iPhone app's setup. Pair URLs and QR codes are credentials.
Without segno the command prints a URL and installation hint.

### Troubleshooting

Run `herdr-remote-bridge doctor`. It checks Python, herdr tools, Tailscale detection
(including the successful method), port, token, optional relay health and APNs config.
A listening port alone does not prove it is the bridge. Tailscale discovery tries
PATH, OS installation paths, then interface addresses in 100.64.0.0/10; an interface
match is a candidate address, not proof of a connected tailnet. On macOS/Windows,
open the Tailscale app and sign in; on Linux run `sudo tailscale up`.
For herdr CLI locations, follow the installed herdr version's CLI setup; doctor
suggests directories to check, and absolute `hc`/`herdr_app` config paths are supported.

From a source checkout, `python3 -m bridge` runs the server; `python3 -m bridge pair` and `python3 -m bridge doctor` run setup and diagnostics. The installed package also supports `python3 -m herdr_remote_bridge`.

## Configuration

Installed and module launches use `~/.config/herdr-remote-bridge/config.json`. Set `HERD_CONFIG` to an absolute config filename to use another directory. Push credentials and saved state live beside that file. Pairing creates the directory and saves a random token if missing, preserving other settings.

For existing Windows/WSL deployments, `python /path/to/bridge/server.py` remains supported and defaults to `config.json` beside that shim. Set `HERD_CONFIG` explicitly when moving between launch methods. Run `python /path/to/bridge/server.py pair` to pair that deployment.

All fields are optional; environment variables override config:

| Field | Environment | Default |
|---|---|---|
| host | HERD_HOST | `tailscale ip -4` |
| port | HERD_PORT | 8787 |
| token | HERD_TOKEN | none (open API; startup warning) |
| hc | HERD_HC | hc on PATH |
| herdr_app | HERD_HERDR_APP | herdr-app on PATH |
| home | HERD_HOME | herdr default folder |

See [config.example.json](config.example.json). Use absolute executable paths for background services. On WSL, Windows executables may be configured as `/mnt/c/Users/<user>/AppData/Local/Programs/<tool>/<tool>.exe`; `home` can be `C:\Users\<user>`. If Tailscale runs on Windows, explicitly configure the Windows Tailscale address.

### Push notifications

To use a shared relay without an Apple key on this computer:

```sh
herdr-remote-bridge pair --relay https://<relay-host>
```

`HERD_RELAY_URL` supplies the default relay. The first call registers this computer and creates private `herd-relay.json`; later pair calls rotate its pair code and save the replacement. Existing phone registrations remain active; scan the new QR for future pairing. A different relay requires a separate config directory. A remote registration/rotation can succeed before a local save fails; inspect local state before retrying after a storage failure.

The QR includes `url`, `token`, hostname `name` and, when configured, `relay`, `bridge`, `pair`. It never includes the bridge secret. The hostname is also the watcher's machine name, so activity registration must use that exact name. Restart serve after pairing/config changes.

Alternatively create `herd-push.json` beside config.json:

```json
{"key_path":"AuthKey_REPLACE.p8","key_id":"REPLACE_KEY_ID","team_id":"REPLACE_TEAM_ID","bundle_id":"app.herdr-remote"}
```

Relative key paths resolve against this directory. `HERD_APNS_KEY_PATH`, `HERD_APNS_KEY_ID`, `HERD_APNS_TEAM_ID` override the file (legacy `APNS_*` also accepted). A relay file takes precedence over direct APNs. Without either file, push sending is disabled. Registration endpoints still persist local tokens. Direct APNs requires Apple credentials, push entitlements and matching device environment; acceptance does not prove display.

Status polling uses the same `hc ls --json` source as `/api/agents`. Blocked/done changes stabilize for four seconds and have a 30-second alert cooldown. Live Activity updates coalesce with a ten-second throttle; push-to-start is attempted once per token per watcher lifetime. Shutdown stops and joins the watcher before closing transport.

## Run in the background

```sh
herdr-remote-bridge service install
herdr-remote-bridge service status
herdr-remote-bridge service uninstall
```

Run as your ordinary user. Installation is repeatable and starts the service now.
It captures the current PATH and absolute config location; rerun after changing
those or moving the pipx installation. Other environment overrides are not captured;
put persistent settings in config.json. The bridge does not start herdr itself.

- macOS: `~/Library/LaunchAgents/app.herdr-remote.bridge.plist`, RunAtLoad and
  KeepAlive, loaded with `launchctl bootstrap gui/$(id -u)`. Output goes to
  `~/Library/Logs/herdr-remote-bridge.log`.
- Linux: `~/.config/systemd/user/herdr-remote-bridge.service`, enabled with
  `systemctl --user enable --now`. View logs with
  `journalctl --user -u herdr-remote-bridge.service`.
- Windows: Task Scheduler task **Herdr Remote Bridge**, current-user interactive
  logon, using pythonw and a wrapper beside config.json to launch `serve` hidden.

### Uninstall

On macOS and Ubuntu/Linux:

```sh
herdr-remote-bridge service uninstall
pipx uninstall herdr-remote-bridge
```

On Windows (PowerShell):

```powershell
herdr-remote-bridge service uninstall
py -m pipx uninstall herdr-remote-bridge
```

Unregister startup before removing pipx. Configuration and pairing credentials are
retained; remove `~/.config/herdr-remote-bridge` (Windows:
`$HOME\.config\herdr-remote-bridge`) yourself if no longer needed. macOS logs
are retained too. Tailscale and herdr are separate installations.

## Security and API

Bind to the computer's Tailscale IP. The bridge also restricts peers to Tailscale IPv4 addresses and loopback. Do not expose it through a public proxy or port forward. Set a token (`pair` does this) so every `/api/*` call requires `Authorization: Bearer <token>`. HTTP relies on Tailscale's encrypted network transport. Without a token, any allowed network peer can operate terminals and browse files.

Keep config.json, herd-relay.json, herd-tokens.json and private keys private; restrict Windows ACLs too. QR codes/pair URLs grant access. Never commit them or enable HTTP debug logging. One bridge process owns its state files. Uploads have a 15 MiB decoded limit and structural JPEG/PNG checks; folder browsing is intended for trusted users. Transcript ownership requires terminal session metadata when multiple terminals share a folder.

[SERVER.md](SERVER.md) is the API reference. [Relay operations](../relay/README.md) are separate from terminal access.

## Offline development checks

```sh
python3 -m unittest discover -s bridge/tests -v
python3 -m compileall -q bridge
```

Tests use fake providers and transports; optional JWT verification skips when its dependencies are unavailable. No live APNs, relay or phone is required.
