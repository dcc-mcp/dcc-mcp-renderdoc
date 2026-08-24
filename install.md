# Install dcc-mcp-renderdoc

## Requirements

- Python 3.9 or newer.
- `dcc-mcp-core` 0.19.45 or newer in the selected Python environment.
- RenderDoc 1.20 or newer, with a matching `renderdoccmd` and `qrenderdoc` pair.
- A loopback connection to the shared DCC-MCP gateway for MCP clients.

Install the published wheel, not a development checkout:

```bash
python -m pip install dcc-mcp-renderdoc
```

The RenderDoc runtime is supported on 64-bit Windows and Linux. The Python wheel and unit tests
also support macOS, but RenderDoc does not publish a macOS desktop runtime. Lifecycle verification
therefore cannot report a directly usable RenderDoc runtime on macOS.

## Supported versions

| Component | Supported floor |
| --- | --- |
| Python | 3.9 |
| dcc-mcp-core | 0.19.45 |
| RenderDoc | 1.20 |

The wheel contains immutable URL and SHA-256 pins for RenderDoc 1.45 on Windows x64 and Linux
x64. The managed installer does not scrape the builds page or resolve a `latest` artifact.

## Agent quick path

Inspect the catalog-backed universal Core plan first when the installed Core release advertises
RenderDoc lifecycle execution:

```bash
dcc-mcp-cli install --dcc-type renderdoc
dcc-mcp-cli install --dcc-type renderdoc --execute --json
```

The adapter-owned surface below performs RenderDoc-specific runtime acquisition and verification.
Every mutating lifecycle verb plans by default. Inspect the Install SOP v1 JSON, then repeat with
`--yes` to execute it:

```bash
dcc-mcp-renderdoc install --json
dcc-mcp-renderdoc install --json --yes
dcc-mcp-renderdoc status --json
dcc-mcp-renderdoc verify --json
```

Use `--dry-run` when the planning intent must be explicit. All lifecycle verbs also accept
`--python <executable>` to bind the exact adapter/Core environment and
`--receipt-path <path>` to select a receipt for automation or testing. The normal receipt is
`~/.dcc-mcp/receipts/renderdoc.json` on every platform.

The JSON follows the public Install SOP v1 schema and always contains the operation status,
adapter/Core versions, per-step results, receipt path, `verify.directly_usable`, and executable
`next_steps`. Stable exit codes are:

| Code | Meaning |
| --- | --- |
| `0` | Operation succeeded or produced a valid plan |
| `10` | Preflight failed |
| `20` | Acquisition failed |
| `30` | Install, receipt, cleanup, or rollback failed |
| `40` | Verification failed |
| `50` | A locked runtime requires restart before retry |

`doctor --json` remains a compatibility preflight. It does not create or verify a lifecycle
receipt; use `status` and `verify` for installed-state truth.

## Manual path

To use a trusted operator-managed RenderDoc installation instead of the pinned managed bundle,
pass the exact `renderdoccmd` path. The matching `qrenderdoc` must be beside it:

```bash
dcc-mcp-renderdoc install --json --dcc-path /opt/renderdoc/bin/renderdoccmd
dcc-mcp-renderdoc install --json --yes --dcc-path /opt/renderdoc/bin/renderdoccmd
```

On Windows, pass the full path to `renderdoccmd.exe`. The installer runs bounded, read-only
`version` probes for both executables, checks the RenderDoc floor and records both executable
digests. It does not copy, modify, or later remove operator-managed binaries.

For the managed path, an operator may override the built-in pin only by setting all three values
below. Partial overrides fail before network access:

```text
DCC_MCP_RENDERDOC_VERSION=<major.minor>
DCC_MCP_RENDERDOC_URL=https://renderdoc.org/stable/<version>/<platform-archive>
DCC_MCP_RENDERDOC_SHA256=<64-character-sha256>
```

The final download URL must remain HTTPS on the approved origin. Downloads and extraction are
bounded by declared and streamed bytes, archive-member count, per-file size, and total expanded
size. Archive paths, links, special files, encrypted entries, duplicate destinations, and digest
mismatches fail closed. Set `DCC_MCP_RENDERDOC_AUTO_DOWNLOAD=0` when acquisition must remain
entirely operator-owned.

On headless Linux, the `qrenderdoc --version` loadability probe uses `xvfb-run` when no display is
configured. Install Xvfb or provide a working X/Wayland display before committing the receipt.

## Verify

```bash
dcc-mcp-renderdoc status --json
dcc-mcp-renderdoc verify --json
dcc-mcp-cli list
```

`status` is read-only and reports a fresh state when no receipt exists. `verify` requires a
receipt and returns `40` when it cannot prove the selected Python environment, adapter/Core
versions, exact executable pair, owned managed-file set and SHA-256 digests, or bounded runtime
probes. A planned install never reports `verify.directly_usable: true`.

The adapter uses an OS-assigned direct port by default and registers it for discovery. MCP clients
should use the shared gateway at `http://127.0.0.1:9765/mcp`. Set
`DCC_MCP_RENDERDOC_PORT` to an integer from 0 through 65535 only when a fixed direct endpoint is
required. After verification, start the external MCP server with:

```bash
dcc-mcp-renderdoc
```

The optional qrenderdoc GUI is for Target Control and manual capture inspection; it is not the
MCP server and does not replace the lifecycle receipt.

## Upgrade

Upgrade the wheel in the same Python environment, inspect the plan, then execute it:

```bash
python -m pip install --upgrade dcc-mcp-renderdoc
dcc-mcp-renderdoc upgrade --json
dcc-mcp-renderdoc upgrade --json --yes
```

`upgrade` first requires and verifies a prior usable receipt. The candidate runtime is downloaded,
bounded, checksum-verified, extracted into a staging directory, probed, atomically installed,
receipted, and verified again. Only after the new state is directly usable may superseded managed
cache versions be removed. A failed candidate or receipt commit preserves the prior receipt and
working runtime.

The managed cache is:

| Platform | Default location |
| --- | --- |
| Windows | `%LOCALAPPDATA%\dcc-mcp\renderdoc` |
| Linux | `${XDG_CACHE_HOME:-~/.cache}/dcc-mcp/renderdoc` |
| Override | `<DCC_MCP_RUNTIME_CACHE>/renderdoc` |

Each managed cache entry owns an internal receipt containing every regular file and its SHA-256.
Cache reuse verifies the complete file set and bytes, then reruns the executable probes.

## Uninstall

Inspect the removal plan, execute it, then remove the wheel:

```bash
dcc-mcp-renderdoc uninstall --json
dcc-mcp-renderdoc uninstall --json --yes
python -m pip uninstall dcc-mcp-renderdoc
```

For a managed runtime, uninstall first verifies the receipt and removes only its exact owned cache
entry plus the lifecycle receipt. A failed removal restores both. For an operator-managed runtime,
it removes only the lifecycle receipt; remove RenderDoc with the package manager that installed
it. Repeating uninstall is safe and reports a fresh state. Stop any adapter or GUI process first;
Windows may return `50` when an executable is locked.

## Troubleshooting

### `renderdoccmd` or `qrenderdoc` is missing, empty, mismatched, or below the floor

Install or upgrade a complete RenderDoc distribution and keep both executables together. Empty
placeholders, environment variables, or version text alone are not usable-state evidence. Run
`install --json --dcc-path <exact-renderdoccmd>` to inspect the bounded probe before committing.

### Managed acquisition is rejected

Do not use a builds page, redirect to another origin, or a `latest` URL. Supply all three override
variables only for an exact official stable archive with a trusted SHA-256. A redirect-origin,
size-limit, archive-structure, or checksum failure does not populate the managed cache.

### Target Control reports no display

Linux requires `DISPLAY`, `WAYLAND_DISPLAY`, or `xvfb-run`; the official bundle does not include
Qt's `offscreen` platform plugin. Windows requires the matching `qrenderdoc.exe`. The lifecycle
probe is read-only and does not connect to a target process.

### Verification reports a receipt or digest mismatch

Do not edit a managed cache in place. Run `status --json`, preserve any prior known-good version,
then use `upgrade --json --yes` to create a new verified transaction. For an operator-managed
runtime, rerun `install` with its exact executable path to record the intended bytes.

### Authentication, configuration, or endpoint discovery fails

This adapter listens on loopback and adds no independent authentication layer. Keep credentials
out of command arguments and diagnostics; configure gateway authentication in `dcc-mcp-core`.
Use `dcc-mcp-cli list` and the shared gateway URL above. If a fixed direct port was configured,
confirm that it is in range and not already owned by another process.
