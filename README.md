# FluidVoice Lite

Lokales Sprache-zu-Text-Diktat für **Linux / Wayland (GNOME)** — inspiriert von
[FluidVoice](https://github.com/altic-dev/FluidVoice) (macOS). Nutzt **dasselbe
Modell** wie FluidVoices Standard-Engine — **NVIDIA Parakeet TDT 0.6B v3**
(25 europäische Sprachen inkl. Deutsch) — hier über `sherpa-onnx` (ONNX int8) auf
der CPU statt CoreML.

Alles läuft **vollständig offline**. Keine Cloud, keine API-Keys.

## Ablauf

**Copilot-Taste** → Aufnahme startet, Live-Overlay zeigt den Text beim Sprechen →
**Copilot-Taste** → Aufnahme stoppt, fertiger Text landet in der Zwischenablage und
wird ins gerade fokussierte Feld eingefügt. OS-weit, in jeder App.

## Warum diese Bausteine

Reines macOS→Linux hat ein paar harte Stellen; so sind sie hier gelöst:

| Aufgabe | Lösung | Warum |
|---|---|---|
| STT | Parakeet TDT v3 via `sherpa-onnx` | identische Gewichte wie FluidVoice, CPU-tauglich |
| Mikrofon | `pw-record` (PipeWire) | nativ auf Fedora |
| Globaler Hotkey | `evdev` (Gruppe `input`) | funktioniert OS-weit, unabhängig vom Compositor |
| Text einfügen | Zwischenablage + `ydotool`-Paste | GNOME kann kein `virtual-keyboard` (wtype scheitert); `ydotool type` verhaut Umlaute auf de-Layout → stattdessen `wl-copy` + `Strg+V` (layout-unabhängig) |
| Overlay | GTK4 | GNOME hat kein `layer-shell` |

Die Copilot-Taste sendet `Meta+Shift+F23`; getriggert wird auf `KEY_F23` (Code 193).

## Voraussetzungen (Fedora)

```bash
sudo dnf install -y wl-clipboard ydotool pipewire-utils python3-gobject gtk4
# Nutzer muss in der Gruppe 'input' sein (für evdev + /dev/uinput):
sudo usermod -aG input "$USER"   # danach neu einloggen
```

## Installation

```bash
git clone <REPO_URL> && cd fluidvoice-lite
uv venv --python 3.14 --system-site-packages .venv   # gi/GTK vom System sichtbar
uv pip install --python .venv/bin/python -r requirements.txt
./scripts/download-model.sh                          # Parakeet v3, ~465 MB
```

## Start

Manuell:
```bash
.venv/bin/python fvld.py
```

Als Autostart (systemd-User-Service):
```bash
cp fluidvoice.service ~/.config/systemd/user/
# Pfade im File ggf. anpassen
systemctl --user enable --now fluidvoice.service
journalctl --user -u fluidvoice -f     # Logs
```

## Konfiguration (Umgebungsvariablen)

| Variable | Default | Zweck |
|---|---|---|
| `FVL_PASTE_KEY` | `ctrl+v` | Einfüge-Kürzel (`ctrl+shift+v` für manche Terminals) |
| `FVL_TRIGGER_CODE` | `193` (F23) | evdev-Keycode des Hotkeys |
| `FVL_DECODE_INTERVAL` | `0.8` | Sekunden zwischen Live-Overlay-Updates |
| `FVL_THREADS` | `6` | CPU-Threads für die Inferenz |
| `FVL_TRAILING_SPACE` | `1` | Leerzeichen ans Ende (fürs Weiterdiktieren) |

## Hilfsskripte

- `detect_key.py` — zeigt evdev-Keycodes, um den Hotkey zu ermitteln
- `fvl.py` — einfache Toggle-Variante ohne Overlay/Daemon (für Tests)

## Grenzen

- Kein echtes Streaming: der Offline-Parakeet dekodiert den laufenden Puffer
  periodisch neu (wirkt live, wird bei langen Diktaten aber träger).
- Overlay-Position ist auf GNOME Wayland nicht frei setzbar (Compositor entscheidet).
- Paste-Kürzel ist app-abhängig (Terminal vs. GUI) — siehe `FVL_PASTE_KEY`.
