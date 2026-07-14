#!/usr/bin/env python3
"""Shows keycodes of all keyboards. Press your desired hotkey (e.g. the
Copilot key, and for comparison the right Ctrl key). Quit with Ctrl+C."""
import sys
from evdev import InputDevice, list_devices, categorize, ecodes

kbds = []
for path in list_devices():
    try:
        d = InputDevice(path)
        caps = d.capabilities()
        if ecodes.EV_KEY in caps and ecodes.KEY_A in caps[ecodes.EV_KEY]:
            kbds.append(d)
            print(f"listening: {d.path}  {d.name}", file=sys.stderr)
    except Exception:
        pass

if not kbds:
    print("No keyboard found (are you in the 'input' group?).", file=sys.stderr)
    sys.exit(1)

print("\n>>> Now press your hotkey. (Ctrl+C to quit)\n", file=sys.stderr)

import selectors
sel = selectors.DefaultSelector()
for d in kbds:
    sel.register(d, selectors.EVENT_READ)

held = set()
try:
    while True:
        for key, _ in sel.select():
            for ev in key.fileobj.read():
                if ev.type != ecodes.EV_KEY:
                    continue
                name = ecodes.KEY.get(ev.code, f"code {ev.code}")
                if ev.value == 1:      # down
                    held.add(name if isinstance(name, str) else name[0])
                    combo = "+".join(sorted(held))
                    print(f"  DOWN  code={ev.code:<4} {name}   [held: {combo}]")
                elif ev.value == 0:    # up
                    held.discard(name if isinstance(name, str) else name[0])
except KeyboardInterrupt:
    print("\ndone.")
