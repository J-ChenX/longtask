"""Windows handle-relative filesystem primitives for release and state tools.

Internal backend, imported only on Windows. Directory descriptors are CRT wrappers
around pinned native handles; callers must use this module for directory operations,
and may use os.read/write/fstat/close for ordinary file descriptors. No global os
monkeypatching or resolve-then-open fallback is used.
"""

from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import errno
import msvcrt
import os
from pathlib import Path
import re
import struct

_kernel = C.WinDLL("kernel32", use_last_error=True)
_native = C.WinDLL("ntdll")
_security = C.WinDLL("advapi32", use_last_error=True)
O_DIRECTORY = 0x10000000  # Backend-only marker, never passed to the CRT.
O_DELETE = 0x20000000  # Request rename/delete rights on a retained source handle.
O_NOFOLLOW = 0
O_NONBLOCK = 0
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.I)


class _Unicode(C.Structure):
    _fields_ = [("length", W.USHORT), ("maximum", W.USHORT), ("buffer", W.LPWSTR)]


class _Attributes(C.Structure):
    _fields_ = [("length", W.ULONG), ("root", W.HANDLE), ("name", C.POINTER(_Unicode)),
                ("flags", W.ULONG), ("security", W.LPVOID), ("quality", W.LPVOID)]


class _Status(C.Structure):
    _fields_ = [("status", C.c_void_p), ("information", C.c_size_t)]


class _Information(C.Structure):
    _fields_ = [("attributes", W.DWORD), ("created", W.FILETIME), ("accessed", W.FILETIME),
                ("written", W.FILETIME), ("volume", W.DWORD), ("size_high", W.DWORD),
                ("size_low", W.DWORD), ("links", W.DWORD), ("index_high", W.DWORD),
                ("index_low", W.DWORD)]


class _Rename(C.Structure):
    _fields_ = [("replace", W.BOOLEAN), ("root", W.HANDLE), ("length", W.DWORD),
                ("name", W.WCHAR * 1)]


class _DirectoryEntry(C.Structure):
    _fields_ = [("next", W.DWORD), ("index", W.DWORD), ("times", C.c_int64 * 4),
                ("end", C.c_int64), ("allocation", C.c_int64), ("attributes", W.DWORD),
                ("length", W.DWORD), ("ea_size", W.DWORD), ("name", W.WCHAR * 1)]


def _signature(library, name, args, result=W.BOOL):
    function = getattr(library, name)
    function.argtypes = args
    function.restype = result
    return function


_create = _signature(_native, "NtCreateFile", [C.POINTER(W.HANDLE), W.DWORD,
    C.POINTER(_Attributes), C.POINTER(_Status), W.LPVOID, W.DWORD, W.DWORD,
    W.DWORD, W.DWORD, W.LPVOID, W.DWORD], C.c_long)
_set = _signature(_native, "NtSetInformationFile", [W.HANDLE, C.POINTER(_Status),
    W.LPVOID, W.DWORD, W.DWORD], C.c_long)
_dos_error = _signature(_native, "RtlNtStatusToDosError", [C.c_long], W.ULONG)
_close = _signature(_kernel, "CloseHandle", [W.HANDLE])
_info = _signature(_kernel, "GetFileInformationByHandle", [W.HANDLE, C.POINTER(_Information)])
_enumerate = _signature(_kernel, "GetFileInformationByHandleEx", [W.HANDLE, C.c_int,
    W.LPVOID, W.DWORD])
_flush = _signature(_kernel, "FlushFileBuffers", [W.HANDLE])
_control = _signature(_kernel, "DeviceIoControl", [W.HANDLE, W.DWORD, W.LPVOID,
    W.DWORD, W.LPVOID, W.DWORD, C.POINTER(W.DWORD), W.LPVOID])
_final_path = _signature(_kernel, "GetFinalPathNameByHandleW", [W.HANDLE, W.LPWSTR,
    W.DWORD, W.DWORD], W.DWORD)
_free = _signature(_kernel, "LocalFree", [W.LPVOID], W.LPVOID)
_process = _signature(_kernel, "GetCurrentProcess", [], W.HANDLE)
_token = _signature(_security, "OpenProcessToken", [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)])
_token_info = _signature(_security, "GetTokenInformation", [W.HANDLE, C.c_int,
    W.LPVOID, W.DWORD, C.POINTER(W.DWORD)])
_sid_text = _signature(_security, "ConvertSidToStringSidW", [W.LPVOID, C.POINTER(W.LPWSTR)])
_parse_security = _signature(_security, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
    [W.LPCWSTR, W.DWORD, C.POINTER(W.LPVOID), C.POINTER(W.DWORD)])
_security_info = _signature(_security, "GetSecurityInfo", [W.HANDLE, C.c_int, W.DWORD,
    C.POINTER(W.LPVOID), W.LPVOID, W.LPVOID, W.LPVOID, C.POINTER(W.LPVOID)], W.DWORD)
_security_text = _signature(_security, "ConvertSecurityDescriptorToStringSecurityDescriptorW",
    [W.LPVOID, W.DWORD, W.DWORD, C.POINTER(W.LPWSTR), C.POINTER(W.DWORD)])


def _wincheck(ok):
    if not ok:
        raise C.WinError(C.get_last_error())


def _ntcheck(status):
    if status < 0:
        raise C.WinError(_dos_error(status))


def _sid_string(sid):
    result = W.LPWSTR()
    _wincheck(_sid_text(sid, C.byref(result)))
    try:
        return result.value
    finally:
        _free(C.cast(result, W.LPVOID))


def _current_sid():
    token = W.HANDLE()
    _wincheck(_token(_process(), 8, C.byref(token)))  # TOKEN_QUERY
    try:
        length = W.DWORD()
        _token_info(token, 1, None, 0, C.byref(length))  # TokenUser
        data = C.create_string_buffer(length.value)
        _wincheck(_token_info(token, 1, data, length, C.byref(length)))
        return _sid_string(C.cast(data, C.POINTER(W.LPVOID))[0])
    finally:
        _close(token)


_SID = _current_sid()
_PRIVATE_DACL = f"D:P(A;;FA;;;SY)(A;;FA;;;{_SID})"


def _component(name):
    name = os.fspath(name)
    if (not isinstance(name, str) or name in {"", ".", ".."}
        or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name)
        or name.rstrip(" .") != name or _RESERVED.fullmatch(name)):
        raise OSError(errno.EINVAL, "unsafe Windows path component", name)
    return name


def _handle(fd):
    return msvcrt.get_osfhandle(fd)


def _open_handle(parent, name, *, directory=None, disposition=1, access=0x100080,
                 private=False, allow_reparse=False, share=3):
    encoded = name.encode("utf-16le")
    if len(encoded) > 65532:
        raise OSError(errno.ENAMETOOLONG, "native path exceeds UNICODE_STRING limit")
    text = C.create_unicode_buffer(name)
    string = _Unicode(len(encoded), len(encoded) + 2, C.cast(text, W.LPWSTR))
    security = W.LPVOID()
    if private:
        _wincheck(_parse_security("O:" + _SID + _PRIVATE_DACL, 1, C.byref(security), None))
    attrs = _Attributes(C.sizeof(_Attributes), parent, C.pointer(string), 0x40, security, None)
    handle = W.HANDLE()
    status = _Status()
    options = 0x200020  # FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT
    if directory is not None:
        options |= 1 if directory else 0x40
    try:
        _ntcheck(_create(C.byref(handle), access, C.byref(attrs), C.byref(status),
            None, 0x80, share, disposition, options, None, 0))
    finally:
        if security:
            _free(security)
    try:
        info = _Information()
        _wincheck(_info(handle, C.byref(info)))
        if info.attributes & 0x400 and not allow_reparse:
            raise OSError(errno.ELOOP, "Windows reparse point is forbidden", name)
        return handle.value, info
    except BaseException:
        _close(handle)
        raise


def _descriptor(handle, flags=0):
    try:
        return msvcrt.open_osfhandle(handle, flags | os.O_BINARY)
    except BaseException:
        _close(handle)
        raise


def open_directory_path(path: Path, *, create=False):
    """Pin a local drive path one component at a time; reject UNC/device aliases."""
    path = Path(path).absolute()
    if not re.fullmatch(r"[A-Za-z]:\\", path.anchor):
        raise OSError(errno.ENOTSUP, "secure Windows traversal requires a local drive path", str(path))
    handle, _ = _open_handle(None, "\\??\\" + path.anchor, directory=True, access=0x100081)
    descriptor = _descriptor(handle)
    try:
        # Check the actual object, not an earlier snapshot of the drive mapping.
        # A SUBST directory can hide ancestors; network mappings have no volume GUID.
        final = C.create_unicode_buffer(32768)
        length = _final_path(_handle(descriptor), final, len(final), 1)  # VOLUME_NAME_GUID
        if not length:
            raise C.WinError(C.get_last_error())
        if length >= len(final) or not re.fullmatch(
            r"\\\\\?\\Volume\{[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\}\\", final.value
        ):
            raise OSError(errno.ENOTSUP, "Windows drive must name a local volume root", path.anchor)
        for part in path.parts[1:]:
            child = open(part, O_DIRECTORY | (os.O_CREAT if create else 0), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open(path, flags, mode=0o777, *, dir_fd=None):
    """Open without following any ancestor or final reparse point; use binary CRT I/O."""
    if dir_fd is None:
        path = Path(path).absolute()
        parent = open_directory_path(path.parent)
        try:
            return open(path.name, flags, mode, dir_fd=parent)
        finally:
            os.close(parent)
    name = _component(path)
    directory = bool(flags & O_DIRECTORY)
    write = bool(flags & (os.O_WRONLY | os.O_RDWR))
    access = 0x120080 | (1 if directory or not flags & os.O_WRONLY else 0)
    if write:
        access |= 0x116  # write/append data, EA and attributes
    if flags & O_DELETE:
        access |= 0x10000
    disposition = 2 if flags & os.O_CREAT and flags & os.O_EXCL else (3 if flags & os.O_CREAT else 1)
    handle, info = _open_handle(_handle(dir_fd), name, directory=directory,
        disposition=disposition, access=access, private=bool(flags & os.O_CREAT and mode in {0o600, 0o700}))
    try:
        # Re-query at the consumer boundary rather than reuse earlier metadata.
        _wincheck(_info(handle, C.byref(info)))
        if info.attributes & 0x400:
            raise OSError(errno.ELOOP, "Windows reparse point is forbidden", name)
        if write and info.links != 1:
            raise OSError(errno.EMLINK, "write target must have exactly one hard link", name)
    except BaseException:
        _close(handle)
        raise
    fd = _descriptor(handle, flags & (os.O_APPEND | os.O_WRONLY | os.O_RDWR))
    try:
        if flags & os.O_TRUNC:
            os.ftruncate(fd, 0)
        return fd
    except BaseException:
        os.close(fd)
        raise


def stat(path, *, dir_fd, follow_symlinks=False):
    if follow_symlinks:
        raise OSError(errno.ENOTSUP, "Windows secure stat never follows reparse points")
    # A retained rename-capable source may already hold DELETE access. This
    # metadata-only handle cannot unpin it; that original handle still denies
    # new DELETE opens. Sharing DELETE here permits inspecting that same object.
    handle, _ = _open_handle(_handle(dir_fd), _component(path), share=7)
    fd = _descriptor(handle)
    try:
        return os.fstat(fd)
    finally:
        os.close(fd)


def artifact_stat(path, *, dir_fd):
    """Observe a leaf's identity, allowing only native symlinks without traversal.

    Runtime/release stat remains strict. Link identity includes its relative flag
    and substitute name, read from the same pinned leaf handle as the metadata.
    Junctions and all other reparse tags remain forbidden.
    """
    name = _component(path)
    handle, info = _open_handle(_handle(dir_fd), name, allow_reparse=True,
                                access=0x120080)
    fd = _descriptor(handle)
    try:
        metadata = os.fstat(fd)
        if not info.attributes & 0x400:
            return metadata, None
        buffer = C.create_string_buffer(16384)
        received = W.DWORD()
        _wincheck(_control(_handle(fd), 0x900A8, None, 0, buffer, len(buffer),
                           C.byref(received), None))  # FSCTL_GET_REPARSE_POINT
        raw = buffer.raw[:received.value]
        if len(raw) < 20:
            raise OSError(errno.EIO, "invalid symbolic link reparse data", name)
        tag, length, _, offset, size, print_offset, print_size, flags = struct.unpack_from(
            "<IHHHHHHI", raw)
        if tag != 0xA000000C:
            raise OSError(errno.ELOOP, "workspace reparse point is not a symbolic link", name)
        if (length + 8 != len(raw) or flags not in {0, 1}
            or any(value % 2 for value in (offset, size, print_offset, print_size))
            or 20 + offset + size > len(raw) or 20 + print_offset + print_size > len(raw)):
            raise OSError(errno.EIO, "invalid symbolic link reparse data", name)
        # Preserve the native substitute spelling and relative/absolute meaning;
        # never open its target or depend on the display-only print name.
        target = raw[20 + offset:20 + offset + size].decode("utf-16le").encode("utf-8")
        final = os.fstat(fd)
        if (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns) != (
            final.st_size, final.st_mtime_ns, final.st_ctime_ns
        ):
            raise OSError(errno.EAGAIN, "symbolic link changed while observing", name)
        return metadata, flags.to_bytes(4, "big") + target
    finally:
        os.close(fd)


def iterdir(fd):
    """Stream pinned directory names so bounded consumers can stop enumeration."""
    buffer = C.create_string_buffer(65536)
    restart = True
    while True:
        if not _enumerate(_handle(fd), 15 if restart else 14, buffer, len(buffer)):
            error = C.get_last_error()
            if error == 18:  # ERROR_NO_MORE_FILES
                return
            raise C.WinError(error)
        restart = False
        offset = 0
        while True:
            entry = _DirectoryEntry.from_buffer(buffer, offset)
            end = offset + _DirectoryEntry.name.offset + entry.length
            if end > len(buffer) or entry.length % 2:
                raise OSError(errno.EIO, "invalid native directory entry")
            name = buffer[offset + _DirectoryEntry.name.offset:end].decode("utf-16le")
            if name not in {".", ".."}:
                yield _component(name)
            if not entry.next:
                break
            if entry.next < _DirectoryEntry.name.offset or offset + entry.next >= len(buffer):
                raise OSError(errno.EIO, "invalid native directory entry offset")
            offset += entry.next


def listdir(fd):
    return list(iterdir(fd))


def canonical_component(fd):
    """Name of this pinned object, excluding DOS 8.3/opened-name aliases."""
    buffer = C.create_unicode_buffer(32768)
    length = _final_path(_handle(fd), buffer, len(buffer), 4)  # NORMALIZED | VOLUME_NAME_NONE
    if not length:
        raise C.WinError(C.get_last_error())
    if length >= len(buffer):
        raise OSError(errno.ENAMETOOLONG, "normalized native name exceeds buffer")
    return _component(buffer.value.rsplit("\\", 1)[-1])


def mkdir(path, mode=0o777, *, dir_fd):
    fd = open(path, os.O_CREAT | os.O_EXCL | O_DIRECTORY, mode, dir_fd=dir_fd)
    os.close(fd)


def rename(source, target, *, src_dir_fd, dst_dir_fd, replace=False):
    """Rename the opened source into a pinned destination directory atomically."""
    name = _component(target)
    handle, info = _open_handle(_handle(src_dir_fd), _component(source), access=0x110080)
    try:
        _rename_handle(handle, name, dst_dir_fd, replace)
    finally:
        _close(handle)


def _rename_handle(handle, target, parent, replace):
    info = _Information()
    _wincheck(_info(handle, C.byref(info)))
    if info.attributes & 0x400:
        raise OSError(errno.ELOOP, "rename source must not be a reparse point")
    if not info.attributes & 0x10 and info.links != 1:
        raise OSError(errno.EMLINK, "rename source must have exactly one hard link")
    encoded = _component(target).encode("utf-16le")
    buffer = C.create_string_buffer(max(C.sizeof(_Rename), _Rename.name.offset + len(encoded)))
    record = _Rename.from_buffer(buffer)
    record.replace = replace
    record.root = _handle(parent)
    record.length = len(encoded)
    C.memmove(C.addressof(buffer) + _Rename.name.offset, encoded, len(encoded))
    status = _Status()
    _ntcheck(_set(handle, C.byref(status), buffer, len(buffer), 10))  # FileRenameInformation


def rename_open(fd, target, *, dst_dir_fd, replace=False):
    """Publish a retained staging descriptor opened with O_DELETE, without reopening."""
    _rename_handle(_handle(fd), target, dst_dir_fd, replace)


def replace(source, target, *, src_dir_fd, dst_dir_fd):
    rename(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, replace=True)


def _remove(path, parent, directory):
    handle, info = _open_handle(_handle(parent), _component(path), directory=directory, access=0x110080)
    try:
        if not directory and info.links != 1:
            raise OSError(errno.EMLINK, "unlink target must have exactly one hard link", path)
        remove = W.BOOLEAN(1)
        status = _Status()
        _ntcheck(_set(handle, C.byref(status), C.byref(remove), 1, 13))  # FileDispositionInformation
    finally:
        _close(handle)


def unlink(path, *, dir_fd):
    _remove(path, dir_fd, False)


def rmdir(path, *, dir_fd):
    _remove(path, dir_fd, True)


def fsync_directory(fd):
    """Flush a writable handle to this directory, failing if the host cannot do it."""
    # The original pinned descriptor remains open throughout. Sharing DELETE
    # in this transient duplicate accommodates its possible O_DELETE access.
    handle, _ = _open_handle(_handle(fd), "", directory=True, access=0x12019F, share=7)
    try:
        _wincheck(_flush(handle))
    finally:
        _close(handle)


def is_private(fd):
    """Recovery accepts only our protected owner+SYSTEM full-control DACL and owner."""
    owner = W.LPVOID()
    descriptor = W.LPVOID()
    error = _security_info(_handle(fd), 1, 5, C.byref(owner), None, None, None, C.byref(descriptor))
    if error:
        raise C.WinError(error)
    try:
        if _sid_string(owner) != _SID:
            return False
        text = W.LPWSTR()
        _wincheck(_security_text(descriptor, 1, 4, C.byref(text), None))
        try:
            return text.value == _PRIVATE_DACL
        finally:
            _free(C.cast(text, W.LPVOID))
    finally:
        _free(descriptor)
