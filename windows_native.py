"""Small, typed Win32 boundary: global shortcuts and layout-neutral paste."""
import ctypes as C
from ctypes import wintypes as W

user32 = C.WinDLL("user32", use_last_error=True)
user32.RegisterHotKey.argtypes = [W.HWND, C.c_int, W.UINT, W.UINT]
user32.RegisterHotKey.restype = W.BOOL
user32.UnregisterHotKey.argtypes = [W.HWND, C.c_int]
user32.UnregisterHotKey.restype = W.BOOL
user32.GetAsyncKeyState.argtypes = [C.c_int]
user32.GetAsyncKeyState.restype = C.c_short
user32.GetForegroundWindow.restype = W.HWND


class KEYBDINPUT(C.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", C.c_size_t)]


class MOUSEINPUT(C.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD),
                ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", C.c_size_t)]


class INPUTUNION(C.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", W.DWORD), ("data", INPUTUNION)]


user32.SendInput.argtypes = [W.UINT, C.POINTER(INPUT), C.c_int]
user32.SendInput.restype = W.UINT


def key_down(code):
    return bool(user32.GetAsyncKeyState(code) & 0x8000)


def modifiers_down():
    return any(key_down(code) for code in (0x10, 0x11, 0x12, 0x5B, 0x5C))


def paste(shortcut="ctrl+v"):
    codes = {"ctrl+v": [0x11, 0x56], "ctrl+shift+v": [0x11, 0x10, 0x56],
             "shift+insert": [0x10, 0x2D]}[shortcut]
    events = [INPUT(type=1, ki=KEYBDINPUT(wVk=code)) for code in codes]
    events += [INPUT(type=1, ki=KEYBDINPUT(wVk=code, dwFlags=2)) for code in reversed(codes)]
    sent = user32.SendInput(len(events), (INPUT * len(events))(*events), C.sizeof(INPUT))
    if sent != len(events):
        # Release anything a partial insertion may have left held down.
        releases = [INPUT(type=1, ki=KEYBDINPUT(wVk=code, dwFlags=2)) for code in reversed(codes)]
        user32.SendInput(len(releases), (INPUT * len(releases))(*releases), C.sizeof(INPUT))
        raise OSError("Einfügen wurde von Windows blockiert. Text liegt in der Zwischenablage.")
