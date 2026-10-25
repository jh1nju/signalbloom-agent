# signalbloom
SignalBloom project for HotTopic - MIT 2026

## SignalBloom agent

The agent is a small Python program that runs on a computer inside a customer's network. It measures network health and sends the results to the SignalBloom cloud app.

- Only makes outbound HTTPS calls to the cloud. It never opens a port on the customer network.
- Registers a site once, using a one-time setup code from Account & Settings.
- Sends latency, packet loss, uptime and device readings every 30 seconds, plus a bandwidth test every 15 minutes.
- Stores readings while the cloud can't be reached and sends them when the connection comes back.
- Collects fixes an Admin has approved (simulated in the prototype) and reports the results.

The contract between the agent and the cloud is [docs/API.md](docs/API.md). The product spec is [docs/SPEC.md](docs/SPEC.md).

> Status: registration, measuring, sending, and collecting approved (simulated) fixes all work.

### Install

Needs Python 3.12 or newer.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

```powershell
copy config.example.yaml config.yaml    # macOS/Linux: cp config.example.yaml config.yaml
```

Edit `config.yaml` and set `cloud_url` and `setup_code`. Leave `site_id` and `site_token` blank, because the agent fills them in when it registers.

**Never commit `config.yaml`.** It holds the site token. It is already listed in `.gitignore`.

### Run

```powershell
python -m signalbloom_agent              # register if needed, then measure and send until Ctrl+C
python -m signalbloom_agent --demo-fault # make readings look like a network fault (for demos)
python -m signalbloom_agent --help
python -m signalbloom_agent --collect-once  # measure once, print the reading, exit (nothing is sent)
```

`--collect-once` prints a body shaped like `POST /api/ingest` in docs/API.md: `{"readings": [...], "devices": [...]}`. Latency and packet loss are measured to `public_address` (default 1.1.1.1). `download_mbps` comes from timing an HTTPS download of `bandwidth_test_mb` megabytes. `devices` comes from pinging every address in the local /24 and reading the ARP table (no admin rights needed). It takes 10–30 seconds. The router (`router_address`, or the default gateway if blank) is shown on a separate "local only" line.

Demo fault: while it is on, each reading gets `demo_fault_latency_ms` (default 300) added to latency and `demo_fault_loss_pct` (default 20, capped at 100) added to packet loss before it is sent. Turn it on with `--demo-fault`, or without a restart by creating a file named `demo_fault` in `data_dir` (delete it to turn the fault off; takes effect within one interval). The cloud is not told, so it treats the readings as a real problem.

With no flags the agent runs until Ctrl+C:

- Every `interval_seconds` (30) it pings `public_address` and sends one reading to `POST /api/ingest` with the current device list. Timing uses a monotonic clock, so it doesn't drift.
- Devices are rechecked every `device_recheck_seconds` (20) and fully rescanned every `device_scan_seconds` (300), in a background thread.
- The bandwidth test runs every `bandwidth_interval_seconds` (900) in the background, started right after a ping. Its result goes into the next reading.
- If the cloud can't be reached or returns a 5xx error, readings are kept in `data_dir/buffer.jsonl` and sent oldest first (up to 500 per request) once it's back. The buffer holds 24 hours of readings and drops the oldest after that.
- A reading the cloud rejects with 400 is saved to `data_dir/rejected.jsonl`. A 401 or 403 stops the agent with instructions to register again.
- After each send it checks `GET /api/agent/commands` for fixes an Admin approved. Only `restart_device`, `isolate_device` and `reconnect_device` are allowed, and all are simulated: the agent finds the device by MAC in its current device list, waits 2 seconds, and reports success (for example `Simulated restart of 192.168.88.5 (6c:94:66:bd:f2:68) completed.`). A MAC not on this network is reported as failed. Any other command type is refused, logged as a warning, and reported as failed. The agent never runs commands from the cloud.
- Each fix runs at most once. Its progress and result are saved in `data_dir/commands.json`, so a result the cloud couldn't receive is sent again next cycle without redoing the fix.
- Each send writes one line to the console and to `data_dir/agent.log`, for example `sent 1 reading, 0 duplicates` or `cloud unreachable, 3 readings buffered`. The site token is never logged.

On first run the agent registers with `setup_code`, saves `site_id` and `site_token` into `config.yaml`, clears `setup_code`, and limits the file to your user account. Later runs skip registration.

### Windows program (no Python needed)

To give the agent to someone who doesn't have Python, build a single Windows program:

```powershell
.\build_exe.ps1        # needs the .venv from Install; output: dist\SignalBloom-Agent.exe
```

The build contains only the agent's code and `config.example.yaml` (as the template). Your `config.yaml`, `data\` and site token are never included, and the script stops with an error if it finds any of them inside the exe.

To use it:

1. Put `SignalBloom-Agent.exe` in its own folder, for example `C:\SignalBloom`.
2. Double-click it. On the first run it asks for the setup code from Account & Settings. Type it and press Enter. It registers with the SignalBloom cloud (`https://signalbloom.ai.studio`) and starts sending readings.
3. Leave the window open. It shows one line per send. Press Ctrl+C to stop.

Later runs skip the setup code and start straight away. If the code is wrong, used or expired, the window says so and waits for Enter. Get a new code and run the program again.

Everything the program creates is in the folder next to the exe: `config.yaml` (holds the site token, so keep it private) and `data\` (log, buffered readings, fix results). To register again, delete `config.yaml`. To turn on the demo fault, create an empty file named `demo_fault` in that `data\` folder, or run `SignalBloom-Agent.exe --demo-fault` from a terminal.

Windows may warn that the program is from an unknown publisher, because it isn't signed. Click **More info**, then **Run anyway**.

### Test

```powershell
python -m pytest
```

### Try it against the fake cloud (testing only)

`tools/fake_cloud.py` is a local stand-in for the cloud app. It implements the four endpoints in docs/API.md and accepts one setup code, `TEST-CODE-0001`, once. Registered sites, received readings, and commands with their results are saved to `tools/.fake_cloud_state.json` (git-ignored), so a restart keeps them; delete that file to start fresh. Devices are kept in memory only.

`http://localhost:8080/debug/summary` shows, for each site, how many readings have arrived, the earliest and latest `measured_at`, the current device list with online status, and each command with its result.

To queue a fix while everything is running, use a MAC from the summary's device list:

```powershell
Invoke-RestMethod -Method Post http://localhost:8080/debug/commands -ContentType application/json `
  -Body '{"type": "restart_device", "target_mac": "aa:bb:cc:dd:ee:01"}'
```

Any `type` is accepted, so you can also queue `"type": "shell"` to see the agent refuse it. Add `"site_id"` if more than one site is registered.

```powershell
python tools/fake_cloud.py               # add --with-command to queue a simulated fix
```

In `config.yaml`, set `cloud_url: http://localhost:8080` and `setup_code: TEST-CODE-0001`, then run the agent in another terminal.

Known limitations are listed in [docs/LIMITATIONS.md](docs/LIMITATIONS.md).
