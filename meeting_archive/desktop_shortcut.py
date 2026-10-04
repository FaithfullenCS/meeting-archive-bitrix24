"""A per-user Start Menu identity for unpackaged Windows toast notifications."""
from __future__ import annotations

import ctypes
import uuid
from contextlib import contextmanager
from pathlib import Path


class GUID(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_string(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]


class Value(ctypes.Union):
    _fields_ = [("pointer", ctypes.c_void_p), ("padding", ctypes.c_uint64 * 2)]


class PropVariant(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_uint16), ("reserved", ctypes.c_uint16 * 3), ("value", Value)]


def checked(result):
    if result < 0:
        raise OSError(result, "Не удалось зарегистрировать ярлык уведомлений Windows")


def call(pointer, index, arguments, *values):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *arguments)(vtable[index])
    return function(pointer, *values)


def query(pointer, iid):
    result = ctypes.c_void_p()
    guid = GUID.from_string(iid)
    checked(call(pointer, 0, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(guid), ctypes.byref(result)))
    return result


@contextmanager
def shell_link():
    api = ctypes.WinDLL("ole32")
    initialized = api.CoInitializeEx(None, 2)
    if initialized not in (0, 1, -2147417850):  # RPC_E_CHANGED_MODE already has a COM apartment.
        checked(initialized)
    pointer = ctypes.c_void_p()
    clsid = GUID.from_string("00021401-0000-0000-c000-000000000046")
    iid = GUID.from_string("000214f9-0000-0000-c000-000000000046")
    api.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_uint32,
                                    ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    try:
        checked(api.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(pointer)))
        yield pointer
    finally:
        if pointer:
            call(pointer, 2, [])
        if initialized in (0, 1):
            api.CoUninitialize()


def notification_shortcut(path: Path, executable: str, arguments: str, app_id: str, activator: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with shell_link() as link:
        checked(call(link, 20, [ctypes.c_wchar_p], executable))
        checked(call(link, 11, [ctypes.c_wchar_p], arguments))
        checked(call(link, 17, [ctypes.c_wchar_p, ctypes.c_int], executable, 0))
        store = query(link, "886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
        try:
            key_guid = GUID.from_string("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3")
            identity = ctypes.create_unicode_buffer(app_id)
            clsid = GUID.from_string(activator)
            for pid, vt, pointer in ((5, 31, ctypes.cast(identity, ctypes.c_void_p)),
                                     (26, 72, ctypes.cast(ctypes.pointer(clsid), ctypes.c_void_p))):
                key = PropertyKey(key_guid, pid)
                value = PropVariant()
                value.vt, value.value.pointer = vt, pointer.value
                checked(call(store, 6, [ctypes.POINTER(PropertyKey), ctypes.POINTER(PropVariant)],
                             ctypes.byref(key), ctypes.byref(value)))
            checked(call(store, 7, []))
        finally:
            call(store, 2, [])
        persist = query(link, "0000010b-0000-0000-c000-000000000046")
        try:
            checked(call(persist, 6, [ctypes.c_wchar_p, ctypes.c_int], str(path), 1))
        finally:
            call(persist, 2, [])
