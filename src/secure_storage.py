"""Windows DPAPI helpers for small local secrets."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


CRYPTPROTECT_UI_FORBIDDEN = 0x1


def dpapi_available() -> bool:
    return os.name == "nt"


if os.name == "nt":
    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


def _blob(data: bytes):
    if not data:
        return _DataBlob(0, None), None
    buffer = ctypes.create_string_buffer(data, len(data))
    return (
        _DataBlob(
            len(data),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        ),
        buffer,
    )


def protect_secret(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is unavailable")
    input_blob, _buffer = _blob(data)
    output_blob = _DataBlob()
    if not _crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "StoneBot Local Secret",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        _kernel32.LocalFree(output_blob.pbData)


def unprotect_secret(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is unavailable")
    input_blob, _buffer = _blob(data)
    output_blob = _DataBlob()
    description = wintypes.LPWSTR()
    if not _crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        _kernel32.LocalFree(output_blob.pbData)
        if description:
            _kernel32.LocalFree(description)
