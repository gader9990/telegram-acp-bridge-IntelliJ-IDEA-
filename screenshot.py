"""Capture the primary Windows monitor as PNG without third-party packages."""
import ctypes
from ctypes import wintypes
import os
import struct
import zlib


def png_from_bgra(width, height, pixels):
    if width <= 0 or height <= 0 or len(pixels) != width * height * 4:
        raise ValueError("Invalid screenshot buffer")
    rows = bytearray()
    for y in range(height):
        source = pixels[y * width * 4:(y + 1) * width * 4]
        row = bytearray(width * 3)
        row[0::3], row[1::3], row[2::3] = source[2::4], source[1::4], source[0::4]
        rows.append(0)  # PNG filter: none
        rows.extend(row)
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('!IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 3)) + chunk(b'IEND', b''))


def capture_screen():
    if os.name != 'nt':
        raise RuntimeError('Скриншоты реализованы только для Windows.')
    user = ctypes.WinDLL('user32', use_last_error=True)
    gdi = ctypes.WinDLL('gdi32', use_last_error=True)
    user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    desktop = user.OpenInputDesktop(0, False, 1)
    if not desktop:
        raise RuntimeError('Рабочий стол недоступен: Windows заблокирована или открыт защищённый экран.')
    try:
        name, needed = ctypes.create_unicode_buffer(256), wintypes.DWORD()
        if not user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
            raise RuntimeError('Не удалось определить активный рабочий стол.')
        if name.value.lower() != 'default':
            raise RuntimeError('Windows заблокирована или открыт защищённый экран. Снимок недоступен.')
    finally:
        user.CloseDesktop(desktop)

    user.GetDC.argtypes, user.GetDC.restype = [wintypes.HWND], wintypes.HDC
    user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    gdi.CreateCompatibleDC.argtypes, gdi.CreateCompatibleDC.restype = [wintypes.HDC], wintypes.HDC
    gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi.SelectObject.argtypes, gdi.SelectObject.restype = [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE
    gdi.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    gdi.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi.StretchBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    class BitmapInfo(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('width', wintypes.LONG), ('height', wintypes.LONG),
                    ('planes', wintypes.WORD), ('bits', wintypes.WORD), ('compression', wintypes.DWORD),
                    ('image_size', wintypes.DWORD), ('xppm', wintypes.LONG), ('yppm', wintypes.LONG),
                    ('used', wintypes.DWORD), ('important', wintypes.DWORD)]
    gdi.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.POINTER(BitmapInfo), wintypes.UINT]
    old_dpi = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    screen = memory = bitmap = previous = None
    try:
        source_w, source_h = user.GetSystemMetrics(0), user.GetSystemMetrics(1)
        if source_w <= 0 or source_h <= 0:
            raise RuntimeError('Нет доступного экрана.')
        scale = min(1, 1600 / source_w, 1600 / source_h)
        width, height = max(1, int(source_w * scale)), max(1, int(source_h * scale))
        screen = user.GetDC(None)
        if not screen:
            raise RuntimeError('Windows не предоставила доступ к изображению экрана.')
        memory = gdi.CreateCompatibleDC(screen)
        bitmap = gdi.CreateCompatibleBitmap(screen, width, height)
        if not memory or not bitmap:
            raise RuntimeError('Не удалось выделить буфер скриншота.')
        previous = gdi.SelectObject(memory, bitmap)
        if not previous or previous == ctypes.c_void_p(-1).value:
            previous = None
            raise RuntimeError('Не удалось подготовить буфер скриншота.')
        gdi.SetStretchBltMode(memory, 4)
        if not gdi.StretchBlt(memory, 0, 0, width, height, screen, 0, 0, source_w, source_h, 0x40CC0020):
            raise RuntimeError('Windows не смогла скопировать экран.')
        gdi.SelectObject(memory, previous)
        previous = None
        info = BitmapInfo()
        info.size, info.width, info.height = ctypes.sizeof(info), width, -height
        info.planes, info.bits = 1, 32
        pixels = ctypes.create_string_buffer(width * height * 4)
        if gdi.GetDIBits(screen, bitmap, 0, height, pixels, ctypes.byref(info), 0) != height:
            raise RuntimeError('Не удалось прочитать изображение экрана.')
        return png_from_bgra(width, height, pixels.raw)
    finally:
        if previous:
            gdi.SelectObject(memory, previous)
        if bitmap:
            gdi.DeleteObject(bitmap)
        if memory:
            gdi.DeleteDC(memory)
        if screen:
            user.ReleaseDC(None, screen)
        if old_dpi:
            user.SetThreadDpiAwarenessContext(old_dpi)
