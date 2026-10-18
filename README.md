# Polypoda

Polypoda is a local web app for [llama.cpp](https://github.com/ggml-org/llama.cpp). It configures a CUDA (or CPU) runtime, searches Hugging Face for GGUF models and vision projectors, downloads them, starts `llama-server`, and can let the loaded model operate **this PC** or **other machines on your private network** (Windows, Linux, Android).

The model always runs on the machine where you start Polypoda. Phone and laptop browsers are remotes for the UI; computer-control actions go to the device you select.

## What it does

- **Launch llama.cpp** with saved settings (model, GPU layers, context, temperature, vision projector, host/port).
- **Search and download GGUF** from Hugging Face into a local `models` folder, with file sizes shown before download.
- **Vision projectors** (`mmproj`) as a separate search mode; downloaded projectors can be selected for image-capable models.
- **Computer use** — the model can take screenshots, move the mouse, type, and run shell commands (PowerShell on Windows, bash on Linux, `adb shell` on Android).
- **Multi-device** — companions on other PCs, plus Android over wireless debugging. Any browser on Tailscale/LAN can open the UI and start a task.
- **Network access** — the launcher binds to `0.0.0.0` and lists Tailscale and LAN URLs so you can open it from a phone or another laptop.

## Requirements

- Python 3.10+
- [llama.cpp](https://github.com/ggml-org/llama.cpp) binaries (for example the official Windows CUDA builds) in a `runtime` folder next to this repo, or another folder you pick in the UI
- A GGUF model (download from the app or point at an existing file)
- Pillow (`pip` installs it with Polypoda)

Optional:

- **NVIDIA GPU** + matching llama.cpp CUDA build
- **Android platform-tools** (`adb` on `PATH`) for phone control
- **Linux companions:** `xdotool`, and ideally `wmctrl` plus `gnome-screenshot` or `scrot`

## Install

```bash
pip install "git+https://github.com/IhebDridi/reimagined-fiesta-personal-llm-polypoda.git"
```

Or from a clone:

```bash
git clone https://github.com/IhebDridi/reimagined-fiesta-personal-llm-polypoda.git
cd reimagined-fiesta-personal-llm-polypoda
pip install -e .
```

## Run the hub (this desktop)

1. Place llama.cpp binaries in `./runtime` (or choose them in the UI).
2. Start the app:

```bash
polypoda
```

Or from a source checkout without installing:

```bash
python -m polypoda
```

3. Open the URL it prints (usually `http://127.0.0.1:47321/`). On the same Tailscale network, use the Tailscale link shown in the header.
4. Pick a model (or download one), start the server, then use **Computer** with a selected device.

Settings are stored under `%APPDATA%\LlamaCppLauncher\` on Windows (and the equivalent config dir pattern used by the app).

## Add another Windows PC

On the other Windows machine:

```bash
pip install "git+https://github.com/IhebDridi/reimagined-fiesta-personal-llm-polypoda.git"
polypoda-companion --hub http://YOUR-HUB-TAILSCALE-IP:47321 --name Laptop
```

Replace the hub URL with the Tailscale (or LAN) address shown in the Polypoda header. The companion prints a URL and token and registers with the hub when reachable. Allow TCP **47340** (companion) and **47321–47339** (hub) through the firewall if needed.

## Add a Linux laptop or PC

On the Linux machine:

```bash
# mouse / keyboard / windows (Debian/Ubuntu example)
sudo apt install xdotool wmctrl gnome-screenshot

pip install "git+https://github.com/IhebDridi/reimagined-fiesta-personal-llm-polypoda.git"
polypoda-companion --hub http://YOUR-HUB-TAILSCALE-IP:47321 --name Linux-Laptop
```

X11 is supported via `xdotool`. Wayland-only sessions may need additional tooling; prefer an X11 session for reliable pointer control.

You can also paste the companion URL and token under **Devices → Add a Linux laptop or PC**.

## Add an Android phone

1. Install [Android platform-tools](https://developer.android.com/tools/releases/platform-tools) and put `adb` on your `PATH` on the hub PC.
2. On the phone: Developer options → Wireless debugging → Pair device with pairing code.
3. In Polypoda **Devices**, enter the pairing host:port and code, then **Connect** with the debugging host:port.
4. Select the phone in **Computer** and give a task. Actions use taps, swipes, typing, and `adb shell`.

## Computer use

1. Start the llama.cpp server from the **Run** card.
2. Choose a device under **Devices** / **Computer**.
3. Describe the task, set max steps or Unlimited, then **Use selected device**.
4. **Stop** cancels the run. Every tool call is logged in the Computer card.

The hub machine must be the one running the model. Opening the UI on a phone only sends the goal; screen/mouse/keyboard run on the selected target.

## Project layout

```
polypoda/           # Installable Python package
  app.py            # Web launcher (hub)
  companion.py      # Remote agent endpoint
  computer*.py      # Windows / Linux control + agent loop
  devices.py        # Device registry (local, companions, Android)
  hf.py             # Hugging Face search / download
  web/              # UI
pyproject.toml
launcher/           # Thin shims for an existing desktop shortcut
```

## License

MIT — see package metadata. llama.cpp binaries and model weights keep their own licenses.
