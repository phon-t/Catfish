# Catfish Pet

A little catfish that swims around your desktop. It wanders on its own,
leaves a faint ripple behind its tail, and can chase your mouse cursor.

![catfish](giphy.webp)

## Download (no Python needed)

Grab the file for your system from the [Releases](../../releases) page:

| System | File |
|---|---|
| Windows | `CatfishPet-windows.exe` |
| macOS (Apple Silicon) | `CatfishPet-macos-arm64.zip` |
| macOS (Intel) | `CatfishPet-macos-x86_64.zip` |
| Linux | `CatfishPet-linux` |

**Windows:** double-click the `.exe`. If SmartScreen shows a blue warning
because the app isn't signed, click **More info** → **Run anyway**.

**macOS:** unzip, then right-click `CatfishPet` → **Open** (the app isn't
signed, so a normal double-click may be blocked the first time). To skip
that check: `xattr -dr com.apple.quarantine CatfishPet.app`.

**Linux:** `chmod +x CatfishPet-linux && ./CatfishPet-linux`. The Linux build
uses a shaped window, so it has no wake ripples and the global shortcuts are
unavailable — use the right-click menu instead.

## Controls

| Action | What happens |
|---|---|
| Mouse wheel / `+` `-` | Make the fish bigger or smaller |
| Drag the fish | Move it somewhere (it resumes swimming after a bit) |
| `Ctrl`+`Shift`+`F` (Windows) | Toggle follow-my-mouse mode (press again to go back to wandering) |
| Right-click the fish | Menu: follow mode, size presets, swim speed, ripples on/off, pause, quit |
| `Space` | Pause / resume |
| `Q` or `Ctrl`+`Shift`+`Q` (Windows) | Quit (`Ctrl`+`Shift`+`Q` works from anywhere) |

To stop it, right-click the fish and pick **Quit**.

Works across multiple monitors — the fish roams the whole desktop.

## Run from source

Needs Python 3.10+ with Pillow:

```bash
pip install pillow
python fish_pet.py
```

Or on Windows just double-click `run_fish.bat`.
