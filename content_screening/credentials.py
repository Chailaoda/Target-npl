"""Project-scoped Windows Credential Manager entry, never global app discovery."""
import ctypes
import os
from ctypes import wintypes


class Credential(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


def library():
    if os.name != "nt":
        raise ValueError("当前凭证存储仅支持 Windows")
    lib = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    lib.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    lib.CredWriteW.restype = wintypes.BOOL
    lib.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
    lib.CredReadW.restype = wintypes.BOOL
    lib.CredFree.argtypes = [ctypes.c_void_p]
    return lib


def save_key(target, key):
    if not key.strip() or len(key.encode("utf-8")) > 2000:
        raise ValueError("密钥为空或过长")
    blob = (ctypes.c_ubyte * len(key.encode("utf-8"))).from_buffer_copy(key.encode("utf-8"))
    cred = Credential(Type=1, TargetName=target, CredentialBlobSize=len(blob), CredentialBlob=blob, Persist=2, UserName="target-npl")
    try:
        if not library().CredWriteW(ctypes.byref(cred), 0):
            raise ValueError("无法写入本项目凭证；未回退到明文文件")
    finally:
        ctypes.memset(blob, 0, len(blob))


def read_key(target):
    lib = library()
    ptr = ctypes.POINTER(Credential)()
    if not lib.CredReadW(target, 1, 0, ctypes.byref(ptr)):
        if ctypes.get_last_error() == 1168:
            raise ValueError("尚未配置本项目方舟 API Key；请在本机运行 configure-ark")
        raise ValueError("无法读取本项目方舟凭证；未读取其他应用凭证")
    try:
        return ctypes.string_at(ptr.contents.CredentialBlob, ptr.contents.CredentialBlobSize).decode("utf-8")
    finally:
        lib.CredFree(ptr)
