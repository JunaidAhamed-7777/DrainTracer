# Plans for the future

1. **Dedicated Report Directory & Unique IDs**
   - Save reports to a dedicated `leak_reports/` folder.
   - Assign a unique UUID to each report (displayed in the TUI on generation) and format filenames as `leak_report_<UUID>.json`.
   - Embed ISO 8601 creation timestamps at the root level of the JSON report.

2. **Target Attachment (`--pid`)**
   - Support attaching DrainTracer to an already running process using its Process ID (`python src/main.py --pid 4820`).
   - *Real-world use case:* Debugging background services, web servers (e.g., FastAPI, Node.js), or long-running database daemons without needing to restart them.

3. **CI/CD & Headless Test Mode (`--headless` / `--timeout`)**
   - Add a non-interactive CLI flag that runs target processes for a set duration (e.g., `--timeout 60s`) without launching the Rich TUI.
   - Return exit code `1` if any `HIGH` or `CRITICAL` leak threshold is breached during execution.
   - *Real-world use case:* Running automated leak regression tests in GitHub Actions pipelines before merging Pull Requests.

4. **Interactive HTML & Visual Chart Reports**
   - Generate self-contained HTML reports alongside JSON exports (`leak_report_<UUID>.html`) featuring interactive line graphs (using Chart.js or Plotly).
   - Visualize memory, handle, and thread trends over time with highlighted red markers where alerts were triggered.

5. **Granular Handle Classification**
   - Categorize Windows handles into specific types (File handles, Socket/Network handles, Registry keys, Thread/Process handles, Mutexes) rather than just a total count.
   - *Real-world use case:* Identifying whether an application is leaking unclosed file streams vs. unclosed database socket connections.

6. **Custom Configuration Profiles (`.drainrc` / `config.toml`)**
   - Allow users to define custom leak thresholds and window durations per project.
   - *Real-world use case:* Game engines or heavy data-processing pipelines legitimate require higher baseline memory slope tolerances than lightweight utility scripts.

7. **Warm-Up / Calibration Phase**
   - Add an initial startup grace period (e.g., `--warmup 10s`) during which resource sampling occurs but leak detection heuristics are deferred.
   - *Real-world use case:* Preventing false-positive alerts triggered by standard application initialization, cache warming, or JIT compilation spikes.

8. **Report Comparison & Diffing Tool (`draintracer diff`)**
   - Add a CLI command to compare two JSON leak reports: `python src/main.py diff reportA.json reportB.json`.
   - Highlight whether a patch reduced memory drift rates or resolved handle leaks between software versions.