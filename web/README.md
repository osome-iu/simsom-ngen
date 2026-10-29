# SimSoM Web Dashboard

See [INITIAL_REQUIREMENTS.md](INITIAL_REQUIREMENTS.md) for the concise
pre-implementation proposal. [REQUIREMENTS.md](REQUIREMENTS.md) contains the
more detailed requirements, simulator contract, and current-to-public system
architecture. [INPUT_OUTPUT_CATALOG.md](INPUT_OUTPUT_CATALOG.md) is the
exhaustive inventory of inputs accepted and outputs produced by the underlying
Python/MPI simulator itself.

The dashboard has a Flask API that can start one local simulator run at a time
and reads the structured snapshots stored in `simsom.analytics_summary`; it does
not parse the simulator log. Its 3D network panel uses `helios-network` and
`helios-web` to render simulator users as nodes. Replies, likes, reposts, and
quotes become directed edges from the acting user to the author of the parent
post. Root posts have no second user endpoint, so the panel reports them as a
separate count instead of drawing synthetic edges.

The graph uses a spread-out GPU force layout. Its on-canvas controls adjust the
Helios link-distance, node-size, and edge-width parameters without rebuilding
the network.

## Running locally

### PostgreSQL preflight

The web API launches the simulator against PostgreSQL, so the database server
must be running and the server must have the `simsom_clock` extension files
installed before a run can start. On an Apple Silicon Mac using Homebrew, the
repository's current PostgreSQL 18 setup is:

```bash
brew services start postgresql@18
export PATH="/opt/homebrew/opt/postgresql@18/bin:$PATH"

pg_isready -d postgres
psql -X -d postgres -c "SELECT 1;"
psql -X -d postgres -tAc \
  "SELECT name, default_version
   FROM pg_available_extensions
   WHERE name = 'simsom_clock';"
```

If the machine uses another supported Homebrew formula, such as
`postgresql@14`, substitute that formula and its matching paths throughout.

The first command should report `accepting connections`, the second should
return `1`, and the extension query should return a `simsom_clock` row. The
connected database role must also be a PostgreSQL superuser because the
simulator drops and recreates the extension at the beginning of a run. If the
server is not accepting connections, inspect and restart the installed formula
with:

```bash
brew services list
# Replace postgresql@18 if a different version is installed.
brew services start postgresql@18
```

If the extension query returns no row, build and install the clock extension
from the repository root:

```bash
make -C libs/simsom/db/clock
make -C libs/simsom/db/clock install
```

When multiple PostgreSQL installations exist, build and install against the
`pg_config` for the same major version as the running server:

```bash
REAL_PG_CONFIG=/opt/homebrew/opt/postgresql@18/bin/pg_config \
  make -C libs/simsom/db/clock clean
REAL_PG_CONFIG=/opt/homebrew/opt/postgresql@18/bin/pg_config \
  make -C libs/simsom/db/clock
REAL_PG_CONFIG=/opt/homebrew/opt/postgresql@18/bin/pg_config \
  make -C libs/simsom/db/clock install
```

If the install step reports `Permission denied`, run it as the user that owns
that PostgreSQL/Homebrew installation or ask an administrator to install the
extension files. Do not run the dashboard or simulator itself with `sudo`.

The API repeats these checks before launching MPI. If PostgreSQL is stopped or
the extension is unavailable, the dashboard reports the setup problem instead
of starting a run that stalls during clock initialization. The simulator
creates the extension object inside the database for each run; the preflight
checks `pg_available_extensions` only to confirm that the server-side extension
files are installed.

For remote databases, use the configured DSN instead of `postgres` and install
the extension files on the database server. The root [README](../README.md#manual-installation)
contains the complete manual PostgreSQL setup instructions.

### Start the dashboard

From the repository root, start the API with the project environment's Python:

```bash
./venv/bin/python -m web.api_server
```

In a second terminal, install the frontend packages once and start Vite:

```bash
cd web/frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173`.

## Configuration

The API defaults to database DSN `postgres` and schema `simsom`. Override them
before starting the API with `SIMSOM_DATABASE_DSN` and
`SIMSOM_DATABASE_SCHEMA`. `SIMSOM_PSQL_BIN` can point to a specific `psql`
executable when more than one PostgreSQL installation is present. If the API is
running on another address, set `SIMSOM_API_PROXY` when starting Vite.

The network endpoint returns at most 50,000 nodes and 100,000 interaction edges
to keep the browser renderer responsive. When the full network exceeds either
limit, the endpoint uses `python-igraph` to select the lowest k-core level that
fits both limits and reports the level used. PostgreSQL streams the endpoint
pairs to a temporary file, so the API does not build a multi-million-edge JSON
object before filtering. Smaller networks are returned without filtering.
Helios requires a modern browser with WebGPU or WebGL2 support.

Choose **Advanced configuration** in the launch card to create a run-specific
configuration without editing the JSONC file. The form exposes population and
quality distribution settings, feed inventory, action distributions, worker
throughput, policy and analytics polling, termination criteria, and final dump
behavior. The API validates those overrides, merges them over the canonical
default config, writes a temporary config for that run, and removes it when the
simulator exits. Database connection and executable settings are intentionally
not editable from the browser.

## Exporting results

After the first analytics snapshot, choose **Export results** beside the
Simulation pulse heading. The dashboard can download:

- A standalone HTML report with summary metrics, an action-growth chart,
  quality and policy signals, network statistics, and the run configuration.
- An analytics CSV containing every snapshot and flattened nested measurements.
- Separate node and edge CSV files for network analysis.
- A complete JSON export containing run state, analytics, network data,
  configuration, and the current log tail.

The HTML report has no external dependencies and can be opened in a browser,
shared as a file, or printed to PDF. Network exports reflect the rows loaded by
the dashboard; the report identifies when the edge set was truncated by the
API's configured limit.
