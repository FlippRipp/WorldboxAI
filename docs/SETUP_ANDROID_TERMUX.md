# Running WorldBox on Android with Termux

WorldBox's heavy lifting (LLM calls) happens on remote APIs, so a phone is a
perfectly viable host: the backend is FastAPI + SQLite and the frontend is a
Vite dev server, both running locally on the device with the browser pointed
at `localhost`.

There are two ways to run it, and both work:

- **Route A (proot Ubuntu):** everything installs exactly like on a normal
  Linux box, including `numba` and `sqlite-vec`, at the cost of a container
  layer and some disk space. Zero surprises.
- **Route B (bare Termux, no Linux container):** lighter and faster, using
  prebuilt Android wheels from the TUR index for the hard packages. The
  `sqlite-vec` SQLite extension ships prebuilt in the repo (`vendor/sqlite-vec/`)
  and loads automatically, and worldgen falls back from `numba` to plain
  numpy.

Pick A if you want the lowest-friction install, B if you want the leanest
setup.

---

## Route A (recommended): Ubuntu via proot-distro

### 1. Install Termux

Install Termux from [F-Droid](https://f-droid.org/packages/com.termux/) or the
[Termux GitHub releases](https://github.com/termux/termux-app/releases). Avoid
old Play Store builds.

### 2. Prepare Termux and Android

```bash
pkg update && pkg upgrade
pkg install proot-distro termux-api
termux-wake-lock
```

`termux-wake-lock` stops Android from suspending the servers when the screen
is off. Also disable battery optimization for Termux in Android settings
(App info → Battery → Unrestricted).

**Android 12+ phantom process killer:** Android silently kills background
child processes (you'll see servers dying with `[Process completed (signal 9)]`).
Fix once via ADB from a PC:

```
adb shell "settings put global settings_enable_monitor_phantom_procs false"
```

On Android 14+ there is also a developer option ("Disable child process
restrictions") that does the same thing.

### 3. Install Ubuntu and enter it

```bash
proot-distro install ubuntu
proot-distro login ubuntu
```

Everything below runs inside the Ubuntu shell.

### 4. Install system dependencies

```bash
apt update
apt install -y git python3 python3-venv python3-pip curl
# Node.js 22 (Ubuntu's default nodejs is too old for Vite)
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt install -y nodejs
```

### 5. Clone and install WorldBox

```bash
git clone https://github.com/FlippRipp/WorldboxAI.git
cd WorldboxAI

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

All requirements — including `numba`, `scipy`, and `sqlite-vec` — have
prebuilt aarch64 wheels, so this should complete without compiling anything.
It still takes a while on a phone; be patient.

```bash
cd frontend
npm install
cd ..
```

### 6. Configure API keys

The repo does not ship keys. Either:

- **Gemini:** `cp backend/.env.example backend/.env` and set `GEMINI_API_KEY`, or
- **OpenRouter:** copy the example config and add your key:

  ```bash
  cp data/providers/openrouter/config.example.json data/providers/openrouter/config.json
  ```

  Then edit `api_key` in that file (or set it later from the in-app Model
  Settings UI).

### 7. Run it

Backend (terminal 1 — open a second Termux session from the notification or
swipe-from-left drawer, and `proot-distro login ubuntu` again):

```bash
cd WorldboxAI
source venv/bin/activate
python3 main.py
```

Frontend (terminal 2):

```bash
cd WorldboxAI/frontend
npm run dev
```

Then open **http://localhost:5173** in Chrome/Firefox on the phone. The Vite
dev server proxies `/api` and `/ws` to the backend on port 8000, so nothing
else needs configuring.

Health check: http://localhost:8000/api/health

### Alternative: single terminal with tmux

```bash
apt install -y tmux
tmux new-session -d -s wb 'cd ~/WorldboxAI && . venv/bin/activate && python3 main.py'
cd ~/WorldboxAI/frontend && npm run dev
```

---

## Route B: bare Termux (no Linux container)

This works without proot — the trick is the [Termux User Repository (TUR)
PyPI index](https://github.com/termux-user-repository/tur), which provides
prebuilt Android wheels for the packages that would otherwise need long
on-device Rust/C compiles (`pydantic-core`, `tiktoken`, `scipy`, `numpy`,
`pillow`, `tokenizers`).

First do the Android prep from Route A step 2 (wake lock, battery exemption,
phantom process killer) — it applies here too.

1. **Install Termux system packages:**

   ```bash
   pkg update
   pkg install python python-numpy python-scipy python-pillow nodejs-lts \
               git clang make rust binutils
   ```

   (`rust` and `clang` are still needed for the few small packages TUR does
   not cover, e.g. `aiohttp` and langgraph's `ormsgpack`.)

2. **Clone the project** into your Termux home directory — not `/sdcard` or
   `~/storage`, which are mounted no-exec and break Python tooling:

   ```bash
   cd ~
   git clone https://github.com/FlippRipp/WorldboxAI.git
   cd WorldboxAI
   ```

3. **Install the Python dependencies via pip with the TUR wheel index.**
   Skip the venv on bare Termux — it adds breakage (a Python upgrade
   invalidates it) for no isolation benefit on a phone. If you insist on one,
   it must be `python -m venv --system-site-packages venv`, or it can't see
   the `pkg`-installed numpy/scipy.

   ```bash
   pip install --extra-index-url https://termux-user-repository.github.io/pypi/ \
               fastapi==0.137.2 uvicorn==0.49.0 python-dotenv==1.2.2 \
               langgraph==1.2.5 litellm==1.89.2 pydantic==2.13.4 \
               websockets==15.0.1 httpx==0.28.1 pytest==9.0.2
   ```

   The heavy packages come down as prebuilt wheels; expect only a couple of
   small source builds. If a Rust build runs out of memory, close other apps
   and retry.

   Note we deliberately do **not** use `requirements.txt` here: it pins
   `numba`, which has no Android build anywhere (not even TUR). That is fine —
   the terrain code in `modules/wb_worldgen` detects the missing import and
   falls back to a pure-numpy implementation (slower worldgen, otherwise
   identical). `sqlite-vec` is also excluded; it gets special treatment next.

4. **`sqlite-vec` — nothing to do.** There is no Bionic wheel, so
   `pip install sqlite-vec` fails — but the repo bundles the official
   prebuilt Android extensions (`vendor/sqlite-vec/`, all four ABIs) and
   the backend loads them automatically whenever `import sqlite_vec`
   fails. If you previously created the manual `sqlite_vec.py` shim in
   the repo root from an older version of this guide, delete it — the
   bundled fallback replaces it. To use a different build, set
   `SQLITE_VEC_PATH=/path/to/vec0.so`.

5. **Install the frontend dependencies:**

   ```bash
   cd ~/WorldboxAI/frontend
   npm install
   cd ..
   ```

6. **Configure API keys** — same as Route A step 6: either
   `cp backend/.env.example backend/.env` and set `GEMINI_API_KEY`, or
   `cp data/providers/openrouter/config.example.json data/providers/openrouter/config.json`
   and set `api_key`.

7. **Run it.** `./start.sh` works on bare Termux: it uses the system
   python when `./venv` is absent, and when a `git pull` brings updates it
   refreshes dependencies with the TUR wheel index, skipping the packages
   that have no Android builds (`sqlite-vec`, `numba`).

   Or run the two servers manually — backend in one Termux session:

   ```bash
   cd ~/WorldboxAI
   python main.py
   ```

   Frontend in a second session (swipe from the left edge → New session):

   ```bash
   cd ~/WorldboxAI/frontend
   npm run dev
   ```

   Open **http://localhost:5173** in the phone's browser.
   Health check: http://localhost:8000/api/health

---

## Tips and caveats

- **Keep Termux alive:** acquire the wake lock (`termux-wake-lock`), exempt
  Termux from battery optimization, and deal with the phantom process killer
  (see Route A step 2), or Android will kill the servers within minutes.
- **Performance:** first `npm install` and `pip install` are slow; the app
  itself is light. Story turns are network-bound (LLM API latency), not
  CPU-bound. Worldgen terrain generation is the only CPU-heavy feature.
- **Storage:** saves live under `data/saves/` inside the repo. In proot the
  repo lives inside the container's filesystem
  (`~/../usr/var/lib/proot-distro/installed-rootfs/ubuntu/root/` from the
  Termux side) — back up saves from there if you reinstall the distro.
- **Access from other devices:** the backend binds `127.0.0.1` only, but the
  Vite dev server listens on all interfaces and proxies to it, so other
  devices on the same Wi-Fi can play at `http://<phone-ip>:5173`.
- **Updating:** `git pull`, then re-run the dependency install if
  dependencies changed — `pip install -r requirements.txt` on Route A, the
  step 3 pip command on Route B — plus `npm install` in `frontend/`.
