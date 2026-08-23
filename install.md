# Install dcc-mcp-renderdoc

## Requirements

- Python 3.9 or newer.
- `dcc-mcp-core` 0.19.45 or newer (installed with the wheel).
- RenderDoc 1.20 or newer, with matching `renderdoccmd` and `qrenderdoc` executables.
- A loopback connection to the shared DCC-MCP gateway for MCP clients.

The RenderDoc runtime is supported on 64-bit Windows and Linux. The Python wheel and unit tests
also support macOS, but RenderDoc does not publish a macOS desktop runtime, so `doctor` and
`verify` intentionally report that platform as not directly usable.

## Supported versions

| Component | Supported floor |
| --- | --- |
| Python | 3.9 |
| dcc-mcp-core | 0.19.45 |
| RenderDoc | 1.20 |

The wheel contains an integrity manifest for RenderDoc 1.45 on Windows x64 and Linux x64. Each
URL is immutable, and each archive must match its pinned SHA-256 before extraction. The adapter
never scrapes the RenderDoc builds page or resolves a `latest` payload.

## Agent quick path

Install the published wheel, then run the machine-readable preflight:

```bash
python -m pip install dcc-mcp-renderdoc
dcc-mcp-renderdoc doctor --json
```

Exit code `0` means the adapter is directly usable. Exit code `10` means that preflight found a
missing or incompatible prerequisite. Apply the structured `next_steps`, then run the stronger
verification surface:

```bash
dcc-mcp-renderdoc verify --json
```

`verify` returns `0` when ready and `40` when verification fails. Both verbs report the adapter,
Core, and RenderDoc versions; executable pairing; display configuration; endpoint configuration;
and machine-readable remediation.

## Manual path

1. Install the wheel with `python -m pip install dcc-mcp-renderdoc`.
2. Install RenderDoc 1.20 or newer from the official RenderDoc distribution or a trusted system
   package.
3. Put `renderdoccmd` on `PATH`, or set `DCC_MCP_RENDERDOC_CMD` to its exact path. Keep
   `qrenderdoc` from the same distribution beside it.
4. On headless Linux, configure a working X/Wayland display (for example Xvfb). RenderDoc's Linux
   bundle does not include Qt's `offscreen` platform plugin.
5. Run `dcc-mcp-renderdoc verify --json`, then start `dcc-mcp-renderdoc`.

Automatic provisioning is enabled by default only for the wheel's built-in immutable manifest.
An operator may replace it only by setting all three values below; partial overrides fail before
network access:

```text
DCC_MCP_RENDERDOC_VERSION=<major.minor>
DCC_MCP_RENDERDOC_URL=https://renderdoc.org/stable/<version>/<platform-archive>
DCC_MCP_RENDERDOC_SHA256=<64-character-sha256>
```

The URL must be the exact official stable archive implied by the platform and version. Set
`DCC_MCP_RENDERDOC_AUTO_DOWNLOAD=0` when runtime acquisition must remain entirely operator-owned.

## Verify

```bash
dcc-mcp-renderdoc doctor --json
dcc-mcp-renderdoc verify --json
dcc-mcp-cli list
```

The adapter uses an OS-assigned direct port by default and registers it for discovery. MCP clients
should use the shared gateway at `http://127.0.0.1:9765/mcp`. Set
`DCC_MCP_RENDERDOC_PORT` to an integer from 0 through 65535 only when a fixed direct endpoint is
required.

## Upgrade

Upgrade the wheel first:

```bash
python -m pip install --upgrade dcc-mcp-renderdoc
```

Upgrade a manually managed RenderDoc installation with the same package manager that installed
it. For an operator pin, update the version, exact URL, and SHA-256 together. A successful verified
installation receives an integrity receipt and atomically replaces the active cache entry; older
adapter-managed entries are removed only after the new command is present. Failed downloads and
checksum mismatches leave no install entry.

The managed cache is:

| Platform | Default location |
| --- | --- |
| Windows | `%LOCALAPPDATA%\dcc-mcp\renderdoc` |
| Linux | `${XDG_CACHE_HOME:-~/.cache}/dcc-mcp/renderdoc` |
| Override | `<DCC_MCP_RUNTIME_CACHE>/renderdoc` |

To force a clean acquisition, stop the adapter and delete only that exact `renderdoc` cache
directory. Unrecognized directories without an adapter integrity receipt are never removed by
automatic cache cleanup.

## Uninstall

Stop the foreground adapter with its normal process supervisor or `Ctrl+C`, then uninstall the
wheel:

```bash
python -m pip uninstall dcc-mcp-renderdoc
```

The adapter installs no host plug-in and creates no persistent daemon. Uninstall RenderDoc with
the package manager that installed it. The managed cache may be deleted separately from the exact
location above; uninstalling the wheel does not remove operator data.

## Troubleshooting

### `renderdoccmd` is missing or below the version floor

Install or upgrade RenderDoc, keep `qrenderdoc` from the same distribution beside it, and set
`DCC_MCP_RENDERDOC_CMD` when the command is not on `PATH`. Re-run `doctor --json`; its version
fields show both installed and minimum versions.

### Automatic acquisition refuses the configured bundle

Do not use the builds page or a `latest` URL. Supply all three operator-pin variables, use the
exact official stable URL for that version/platform, and obtain the SHA-256 through a trusted
release process. A checksum mismatch is terminal and does not populate the install cache.

### Target Control reports no display or no `qrenderdoc`

Windows requires the matching `qrenderdoc.exe`. Linux requires matching `qrenderdoc` plus
`DISPLAY`, `WAYLAND_DISPLAY`, or an explicitly working `QT_QPA_PLATFORM`. Headless CI should run
under Xvfb with the `xcb` platform.

### Authentication or configuration fails

This standalone adapter listens on loopback and adds no independent authentication layer. Do not
put credentials in command arguments or environment diagnostics. Configure any gateway
authentication in `dcc-mcp-core`; `doctor --json` reports the local endpoint contract and whether
the adapter-specific port setting is valid.

### The MCP endpoint is not discovered

Use `dcc-mcp-cli list` and connect through `http://127.0.0.1:9765/mcp`. If a fixed direct port was
configured, confirm that `DCC_MCP_RENDERDOC_PORT` is an integer from 0 through 65535 and that no
other process owns it. The adapter does not require or probe a remote mutable endpoint.
