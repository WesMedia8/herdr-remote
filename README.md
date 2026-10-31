# Herdr Remote bridge

The bridge connects the **Herdr Remote** iPhone app to herdr agents (Claude Code, Codex, shells) running on your computer. It runs on **macOS, Linux and Windows**.

> Herdr Remote is an independent project and is not affiliated with herdr.

## Get the app

Join the iPhone beta on TestFlight: https://testflight.apple.com/join/EdbkSmNd

## Install the bridge

1. Install [Tailscale](https://tailscale.com/download) on your computer **and** your iPhone, and sign in to the same account.
2. Run the installer on the computer where herdr runs:

**macOS / Linux**
```sh
curl -fsSL https://raw.githubusercontent.com/WesMedia8/herdr-remote/main/bridge/install/install.sh | sh
```

**Windows (PowerShell)**
```powershell
irm https://raw.githubusercontent.com/WesMedia8/herdr-remote/main/bridge/install/install.ps1 | iex
```

3. Scan the QR code it shows with the Herdr Remote app.

Something not working? Run `herdr-remote-bridge doctor`.

Full documentation: [bridge/README.md](bridge/README.md) · API reference: [bridge/SERVER.md](bridge/SERVER.md)
