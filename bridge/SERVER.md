# Herdr Remote bridge API

Runs where herdr runs, using `hc` and `herdr-app`. Python 3.10+.
See [README.md](README.md) for installation, configuration and service templates.

## Access and responses

Bind to the Tailscale IPv4 address by default. Only Tailscale IPv4 peers
(`100.64.0.0/10`) and loopback may connect; other peers receive 403.
When `token` in config.json or `HERD_TOKEN` is set, **every `/api/*` request**
requires `Authorization: Bearer <token>`. The comparison is constant-time;
missing/incorrect credentials return `401 {"error":"unauthorized"}`.
Without a token, API access is open to allowed peers and serve prints a warning.
`pair` creates and saves a token when absent. Restart serve after config changes.

Responses use JSON with Content-Length and no-store caching. Errors are
`{"error":"..."}`. Unknown paths and wrong-method API calls return JSON 404
(after auth). GET `/` returns `{"app":"herd-phone","ok":true}` without token auth.
POST requires a nonnegative Content-Length and a JSON object; transfer encoding
is unsupported. Bodies over 20,000 bytes (21 MiB for upload) return 413. Invalid
JSON returns 400. Storage failures return 500 with a sanitized error.

## Terminal and project routes

| Method | Path | Input | Result |
|---|---|---|---|
| GET | `/api/agents` | none | full `hc ls --json` object |
| GET | `/api/read` | `?t=t7` | `{"text":"..."}`, 300 lines with ANSI removed |
| POST | `/api/send` | `{"t":"t7","text":"hello"}` | `{"ok":true}` |
| POST | `/api/keys` | `{"t":"t7","key":"enter"}` | `{"ok":true}` |
| GET | `/api/spaces` | none | `{"spaces":[{"name":"Project","cwd":"/path/to/project"}]}` |
| POST | `/api/new` | `{"space":"Project","agent":"codex","cwd":"/path/to/project","same_space":false}` | `{"address":"t9","where":"Project / 1"}` |
| POST | `/api/space` | `{"space":"Project","cwd":"/path/to/project"}` | `{"ok":true}` |
| GET | `/api/dirs` | optional `?path=/path/to/folder` | `{"path":"...","parent":"...","dirs":["child"]}` |
| GET | `/api/chat` | `?t=t7`, optional `&since=<message-id>` | `{"messages":[{"id":"...","role":"assistant","text":"...","ts":"..."}]}` |
| POST | `/api/upload` | `{"t":"t7","filename":"photo.jpg","data":"<base64>"}` | `{"path":"<absolute saved path>"}` |

Terminal addresses match `^t[0-9a-z]{1,8}$`. Send uses hc JSON-RPC without a
caller and removes inherited HC_TERMINAL_ID from the child environment. Empty
text is rejected. Keys: enter, esc, tab, up, down, left, right, ctrl+c, y, n,
1, 2, 3, space, backspace.

New agents accept `claude-code`, `codex`, or `sh`. The default creates a new
space, suffixing a number if the name is already live. Only `same_space:true`
adds a tab to an existing space. Folder precedence: explicit cwd, saved project
cwd, live space folder, configured home, then herdr's default. Selected folders
must exist. Live spaces determine existence and name collisions; saved-only
projects do not count as running spaces. Spaces merge case-insensitively: live
names and nonempty folders win, saved cwd fills gaps, and saved-only projects
remain selectable. `/api/space` validates and saves a project default, then
creates the herdr space if missing. If herdr creation fails, the saved default
remains and the response reports failure; retry can ensure the space exists.

Dirs defaults to home and permits browsing within its resolved drive root,
hiding dot/system folders and escaping symlinks. Parent is null at that root.
Uploads require a live terminal, allow JPEG/PNG, reject traversal/reserved
names, structurally validate image data, and limit decoded data to 15 MiB.
Files are created exclusively under `~/herd-uploads`; collisions get unique names.
The client sends the returned path separately through `/api/send`.

Chat resolves trusted terminal metadata (`agent`, `cwd`, `session_file`,
`session_id`) from the same hc snapshot. It never accepts a client transcript
path. Claude's newest-file fallback is allowed only for a sole terminal with a
known cwd; shared folders require explicit session mapping. Codex requires a
session ID or file. Unsupported/missing transcripts return 4xx for app fallback.
Messages have stable IDs, user/assistant/tool roles, optional ISO timestamp,
and at most 200 records in order. Tool results/reasoning are omitted. Unknown
`since` returns the current last 200 messages.

## Push routes

| Method | Path | Input | Result |
|---|---|---|---|
| POST | `/api/register` | `{"token":"<hex>","env":"development"}` | `{"ok":true}` |
| POST | `/api/activity` | `{"token":"<hex>","env":"production","machine":"<hostname>"}` | `{"ok":true}` |
| POST | `/api/activity` | `{"start_token":"<hex>","env":"production"}` | `{"ok":true}` |
| POST | `/api/test-push` | `{}` | `{"sent":0,"errors":[]}` or 503 if unconfigured |

Activity may supply both token kinds; update tokens require machine. Environment
is strictly development or production; tokens are nonempty hex. Registrations
are idempotent and persist separately from credentials. Test-push counts accepted
notifications, not display on the phone. Relay mode sends one fan-out request;
local registration routes still store locally, while the app must register its
relay tokens directly with the relay using the QR pairing details.

At startup herd-relay.json selects RelayClient; otherwise herd-push.json selects
direct APNsClient; otherwise there is no push client or watcher. The watcher polls
`live_terminals()` from the shared agents provider, seeds statuses silently,
and stops/joins before transport shutdown. Relay credentials, APNs setup and
notification timing are documented in [README.md](README.md).
