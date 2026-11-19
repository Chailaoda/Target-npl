"""Hide or reveal the dedicated Chrome windows of one browser process (Windows only).

Collection runs in a real, headed Chrome whose windows are kept off-screen and
hidden, so platforms see an ordinary browser. The same window is revealed only
when the user has to act (login, verification). Other platforms are no-ops.
"""
import os
import time

SW_HIDE, SW_SHOWNORMAL = 0, 1
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x1, 0x2, 0x40


def _user32():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    return ctypes, wintypes, user32


def main_windows(pid):
    """Top-level browser frames owned by this Chrome process (visible or hidden)."""
    if os.name != 'nt' or type(pid) is not int or pid < 1:
        return []
    ctypes, wintypes, user32 = _user32()
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and not user32.GetWindow(hwnd, 4) and user32.GetWindowTextLengthW(hwnd) > 0:
            name = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, name, 64)
            if name.value == 'Chrome_WidgetWin_1':
                found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return found


def hide(pid, wait=0.0):
    """Hide every visible frame; optionally wait for the first frame to appear."""
    deadline = time.monotonic() + wait
    while True:
        windows = main_windows(pid)
        if windows:
            _, _, user32 = _user32()
            for hwnd in windows:
                if user32.IsWindowVisible(hwnd):
                    user32.ShowWindow(hwnd, SW_HIDE)
            return len(windows)
        if time.monotonic() >= deadline:
            return 0
        time.sleep(0.1)


def reveal(pid, wait=0.0):
    """Move frames onto the primary work area, show them and raise them on top."""
    deadline = time.monotonic() + wait
    while True:
        windows = main_windows(pid)
        if windows or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    if not windows:
        return 0
    ctypes, wintypes, user32 = _user32()
    try:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    area = wintypes.RECT()
    user32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0)
    width, height = area.right - area.left, area.bottom - area.top
    w, h = min(1440, int(width * .82)), min(980, int(height * .88))
    x, y = area.left + (width - w) // 2, area.top + (height - h) // 2
    for index, hwnd in enumerate(windows):
        offset = 28 * index
        user32.SetWindowPos(hwnd, 0, x + offset, y + offset, w, h, 0)
        user32.ShowWindow(hwnd, SW_SHOWNORMAL)
        # Raise above other windows without requiring foreground permission.
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetForegroundWindow(hwnd)
    return len(windows)
