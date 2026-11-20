"""Chamadas à API do Windows via ctypes (sem dependências externas).

Processos (listar, encerrar, iniciar), dono de uma porta TCP, pastas do
usuário e instância única.
"""
from __future__ import annotations

import ctypes
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes

from .log import log

LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800


def _system_dll(name: str) -> ctypes.WinDLL:
    # Só da pasta do sistema. O app roda como administrador a partir de onde o
    # usuário o salvou (Downloads, por exemplo); a busca padrão de DLLs começa
    # pela pasta do executável, onde qualquer programa pode ter deixado um
    # arquivo com o mesmo nome.
    return ctypes.WinDLL(name, use_last_error=True, winmode=LOAD_LIBRARY_SEARCH_SYSTEM32)


kernel32 = _system_dll("kernel32")
user32 = _system_dll("user32")
advapi32 = _system_dll("advapi32")
shell32 = _system_dll("shell32")
ole32 = _system_dll("ole32")
userenv = _system_dll("userenv")
iphlpapi = _system_dll("iphlpapi")
wtsapi32 = _system_dll("wtsapi32")
ntdll = _system_dll("ntdll")

SIZE_T = ctypes.c_size_t
LPVOID = wintypes.LPVOID
HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD
BOOL = wintypes.BOOL

INVALID_HANDLE_VALUE = HANDLE(-1).value
ERROR_ACCESS_DENIED = 5
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_ALREADY_EXISTS = 183

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001
PROCESS_CREATE_PROCESS = 0x0080
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_IMPERSONATE = 0x0004
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_ADJUST_DEFAULT = 0x0080
SE_PRIVILEGE_ENABLED = 0x00000002
SE_GROUP_INTEGRITY = 0x00000020
LUA_TOKEN = 0x4                         # CreateRestrictedToken: token "filtrado", como o do UAC
TOKEN_DEFAULT_DACL_CLASS = 6
TOKEN_LOGON_SID_CLASS = 28
TOKEN_INTEGRITY_LEVEL_CLASS = 25
MEDIUM_INTEGRITY_SID = "S-1-16-8192"
SDDL_REVISION_1 = 1
PROCESS_COMMAND_LINE_INFORMATION = 60
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NEW_PROCESS_GROUP = 0x00000200
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
PROC_THREAD_ATTRIBUTE_PARENT_PROCESS = 0x00020000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
AF_INET = 2
TCP_TABLE_OWNER_PID_LISTENER = 3


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", DWORD),
        ("cntUsage", DWORD),
        ("th32ProcessID", DWORD),
        ("th32DefaultHeapID", SIZE_T),
        ("th32ModuleID", DWORD),
        ("cntThreads", DWORD),
        ("th32ParentProcessID", DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", DWORD),
        ("dwLocalAddr", DWORD),
        ("dwLocalPort", DWORD),
        ("dwRemoteAddr", DWORD),
        ("dwRemotePort", DWORD),
        ("dwOwningPid", DWORD),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", LPVOID), ("Attributes", DWORD)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


class TOKEN_DEFAULT_DACL(ctypes.Structure):
    _fields_ = [("DefaultDacl", LPVOID)]


class TOKEN_GROUPS_1(ctypes.Structure):
    """TOKEN_GROUPS com um grupo (o ctypes cuida do alinhamento de Groups em 64 bits)."""
    _fields_ = [("GroupCount", DWORD), ("Groups", SID_AND_ATTRIBUTES * 1)]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", LPVOID)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", LPVOID)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", DWORD),
        ("dwThreadId", DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", SIZE_T),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


def _declare(dll, name, restype, *argtypes):
    function = getattr(dll, name)
    function.restype = restype
    function.argtypes = argtypes
    return function


_declare(kernel32, "CloseHandle", BOOL, HANDLE)
_declare(kernel32, "CreateToolhelp32Snapshot", HANDLE, DWORD, DWORD)
_declare(kernel32, "Process32FirstW", BOOL, HANDLE, ctypes.POINTER(PROCESSENTRY32W))
_declare(kernel32, "Process32NextW", BOOL, HANDLE, ctypes.POINTER(PROCESSENTRY32W))
_declare(kernel32, "OpenProcess", HANDLE, DWORD, BOOL, DWORD)
_declare(kernel32, "TerminateProcess", BOOL, HANDLE, wintypes.UINT)
_declare(kernel32, "WaitForSingleObject", DWORD, HANDLE, DWORD)
_declare(kernel32, "ProcessIdToSessionId", BOOL, DWORD, ctypes.POINTER(DWORD))
_declare(kernel32, "GetCurrentProcessId", DWORD)
_declare(kernel32, "GetCurrentProcess", HANDLE)
_declare(kernel32, "CreateMutexW", HANDLE, LPVOID, BOOL, wintypes.LPCWSTR)
_declare(kernel32, "SetDllDirectoryW", BOOL, wintypes.LPCWSTR)
_declare(kernel32, "InitializeProcThreadAttributeList", BOOL, LPVOID, DWORD, DWORD, ctypes.POINTER(SIZE_T))
_declare(kernel32, "UpdateProcThreadAttribute", BOOL, LPVOID, DWORD, SIZE_T, LPVOID, SIZE_T, LPVOID, LPVOID)
_declare(kernel32, "DeleteProcThreadAttributeList", None, LPVOID)
_declare(kernel32, "CreateProcessW", BOOL, wintypes.LPCWSTR, wintypes.LPWSTR, LPVOID, LPVOID, BOOL, DWORD,
         LPVOID, wintypes.LPCWSTR, LPVOID, ctypes.POINTER(PROCESS_INFORMATION))
_declare(kernel32, "CreateJobObjectW", HANDLE, LPVOID, wintypes.LPCWSTR)
_declare(kernel32, "SetInformationJobObject", BOOL, HANDLE, ctypes.c_int, LPVOID, DWORD)
_declare(kernel32, "AssignProcessToJobObject", BOOL, HANDLE, HANDLE)
_declare(user32, "GetShellWindow", wintypes.HWND)
_declare(user32, "GetWindowThreadProcessId", DWORD, wintypes.HWND, ctypes.POINTER(DWORD))
_declare(advapi32, "OpenProcessToken", BOOL, HANDLE, DWORD, ctypes.POINTER(HANDLE))
_declare(advapi32, "LookupPrivilegeValueW", BOOL, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID))
_declare(advapi32, "AdjustTokenPrivileges", BOOL, HANDLE, BOOL, ctypes.POINTER(TOKEN_PRIVILEGES), DWORD, LPVOID, LPVOID)
_declare(shell32, "IsUserAnAdmin", BOOL)
_declare(shell32, "SHGetKnownFolderPath", ctypes.c_long, ctypes.POINTER(GUID), DWORD, HANDLE, ctypes.POINTER(LPVOID))
_declare(ole32, "CoTaskMemFree", None, LPVOID)
_declare(userenv, "CreateEnvironmentBlock", BOOL, ctypes.POINTER(LPVOID), HANDLE, BOOL)
_declare(userenv, "DestroyEnvironmentBlock", BOOL, LPVOID)
_declare(iphlpapi, "GetExtendedTcpTable", DWORD, LPVOID, ctypes.POINTER(DWORD), BOOL, wintypes.ULONG, ctypes.c_int, wintypes.ULONG)
_declare(kernel32, "GetSystemDirectoryW", wintypes.UINT, wintypes.LPWSTR, wintypes.UINT)
_declare(kernel32, "LocalFree", HANDLE, HANDLE)
_declare(advapi32, "GetTokenInformation", BOOL, HANDLE, ctypes.c_int, LPVOID, DWORD, ctypes.POINTER(DWORD))
_declare(advapi32, "LookupAccountNameW", BOOL, wintypes.LPCWSTR, wintypes.LPCWSTR, LPVOID, ctypes.POINTER(DWORD),
         wintypes.LPWSTR, ctypes.POINTER(DWORD), ctypes.POINTER(ctypes.c_int))
_declare(advapi32, "ConvertSidToStringSidW", BOOL, LPVOID, ctypes.POINTER(LPVOID))
_declare(advapi32, "ConvertStringSidToSidW", BOOL, wintypes.LPCWSTR, ctypes.POINTER(LPVOID))
_declare(advapi32, "GetLengthSid", DWORD, LPVOID)
_declare(advapi32, "CreateRestrictedToken", BOOL, HANDLE, DWORD, DWORD, LPVOID, DWORD, LPVOID, DWORD, LPVOID,
         ctypes.POINTER(HANDLE))
_declare(advapi32, "SetTokenInformation", BOOL, HANDLE, ctypes.c_int, LPVOID, DWORD)
_declare(advapi32, "CreateProcessAsUserW", BOOL, HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, LPVOID, LPVOID, BOOL,
         DWORD, LPVOID, wintypes.LPCWSTR, LPVOID, ctypes.POINTER(PROCESS_INFORMATION))
_declare(wtsapi32, "WTSQuerySessionInformationW", BOOL, HANDLE, DWORD, ctypes.c_int,
         ctypes.POINTER(LPVOID), ctypes.POINTER(DWORD))
_declare(wtsapi32, "WTSFreeMemory", None, LPVOID)
_declare(advapi32, "ConvertStringSecurityDescriptorToSecurityDescriptorW", BOOL, wintypes.LPCWSTR, DWORD,
         ctypes.POINTER(LPVOID), ctypes.POINTER(wintypes.ULONG))
_declare(advapi32, "GetSecurityDescriptorDacl", BOOL, LPVOID, ctypes.POINTER(BOOL), ctypes.POINTER(LPVOID),
         ctypes.POINTER(BOOL))
_declare(ntdll, "NtQueryInformationProcess", ctypes.c_long, HANDLE, ctypes.c_int, LPVOID, wintypes.ULONG,
         ctypes.POINTER(wintypes.ULONG))

FOLDERID_LOCAL_APP_DATA = GUID.parse("F1B32785-6FBA-4FCF-9D55-7B8E7F157091")
FOLDERID_PROFILE = GUID.parse("5E6C858F-0E22-4760-9AFE-EA3317B67173")
FOLDERID_PROGRAM_FILES = GUID.parse("905E63B6-C1BF-494E-B29C-65B732D3D21A")
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTS_USER_NAME = 5
WTS_DOMAIN_NAME = 7
TOKEN_USER_CLASS = 1


def _win_error(what: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{what}: {ctypes.FormatError(code).strip()}")


# --- Privilégios e instância única --------------------------------------------

def is_elevated() -> bool:
    return bool(shell32.IsUserAnAdmin())


_instance_mutex = None


def acquire_single_instance(name: str) -> bool:
    """False se já existe outra janela do app aberta nesta sessão do Windows."""
    global _instance_mutex
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
    error = ctypes.get_last_error()
    if not handle:
        # Acesso negado = o mutex existe e pertence a uma instância elevada.
        return error != ERROR_ACCESS_DENIED
    if error == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _instance_mutex = handle  # fica aberto até o processo terminar
    return True


def set_dpi_awareness() -> None:
    """Evita a janela borrada em telas com escala acima de 100%."""
    try:
        _system_dll("shcore").SetProcessDpiAwareness(1)
    except (OSError, AttributeError):
        try:
            user32.SetProcessDPIAware()
        except (OSError, AttributeError):
            pass


# --- Processos -----------------------------------------------------------------

def list_processes() -> list[tuple[int, int, str]]:
    """[(pid, pid_do_pai, nome_do_exe)] de todos os processos."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, INVALID_HANDLE_VALUE):
        raise _win_error("CreateToolhelp32Snapshot")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        found = []
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            found.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return found
    finally:
        kernel32.CloseHandle(snapshot)


def process_name(pid: int) -> str | None:
    for found_pid, _, name in list_processes():
        if found_pid == pid:
            return name
    return None


def _session_of(pid: int) -> int | None:
    session = DWORD()
    if kernel32.ProcessIdToSessionId(pid, ctypes.byref(session)):
        return session.value
    return None


def in_current_session(pid: int) -> bool:
    """True se o processo está na mesma sessão do Windows que o launcher."""
    return _session_of(pid) == _session_of(kernel32.GetCurrentProcessId())


def pids_by_name(exe_name: str) -> list[int]:
    """PIDs dos processos `exe_name` na sessão do Windows do usuário atual."""
    wanted = exe_name.lower()
    mine = _session_of(kernel32.GetCurrentProcessId())
    return [
        pid for pid, _, name in list_processes()
        if name.lower() == wanted and _session_of(pid) == mine
    ]


def kill_pid(pid: int, wait_ms: int = 2000) -> bool:
    """Encerra o processo. True se ele terminou (ou já não existia)."""
    handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
    error = 0 if handle else ctypes.get_last_error()
    if error == ERROR_ACCESS_DENIED and desktop_user().other_account and _enable_privilege("SeDebugPrivilege"):
        # Elevado com OUTRA conta, os processos do usuário da área de trabalho só se
        # deixam encerrar com o privilégio de depuração. Com a mesma conta eles já
        # podem ser encerrados: ali, "acesso negado" é processo de outra conta ou de
        # um serviço, que o launcher não deve tocar.
        handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
        error = 0 if handle else ctypes.get_last_error()
    if not handle:
        return error != ERROR_ACCESS_DENIED      # outro erro: o processo já não existe
    try:
        kernel32.TerminateProcess(handle, 1)
        return kernel32.WaitForSingleObject(handle, wait_ms) == 0
    finally:
        kernel32.CloseHandle(handle)


def process_command_line(pid: int) -> str | None:
    """Linha de comando com que o processo foi iniciado (None se não der para ler)."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.ULONG(0)
        ntdll.NtQueryInformationProcess(handle, PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if ntdll.NtQueryInformationProcess(
            handle, PROCESS_COMMAND_LINE_INFORMATION, buffer, size, ctypes.byref(size)
        ) < 0:
            return None
        text = UNICODE_STRING.from_buffer(buffer)
        return ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""
    finally:
        kernel32.CloseHandle(handle)


def kill_by_name(exe_name: str, timeout: float = 6.0) -> int:
    """Encerra todos os processos `exe_name` da sessão. Devolve quantos encerrou.

    Lança OSError se algum sobreviver (por exemplo, um processo elevado quando
    o app está rodando sem privilégio de administrador).
    """
    deadline = time.monotonic() + timeout
    killed = 0
    while True:
        pids = pids_by_name(exe_name)
        if not pids:
            return killed
        if time.monotonic() > deadline:
            raise OSError(f"não foi possível encerrar {exe_name}")
        for pid in pids:
            if kill_pid(pid):
                killed += 1
        time.sleep(0.2)


# --- Porta TCP ------------------------------------------------------------------

def tcp_listener_pids(port: int) -> list[int]:
    """PIDs dos processos que escutam a porta TCP (IPv4) informada."""
    size = DWORD(0)
    buffer = None
    for _ in range(5):
        buffer = ctypes.create_string_buffer(size.value) if size.value else None
        result = iphlpapi.GetExtendedTcpTable(
            buffer, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0
        )
        if result == 0:
            break
        if result != ERROR_INSUFFICIENT_BUFFER:
            return []
    else:
        return []
    if buffer is None or size.value < 4:
        return []
    count = struct.unpack_from("<I", buffer, 0)[0]
    row_size = ctypes.sizeof(MIB_TCPROW_OWNER_PID)
    count = min(count, (len(buffer) - 4) // row_size)
    rows = (MIB_TCPROW_OWNER_PID * count).from_buffer_copy(buffer, 4)
    return [
        row.dwOwningPid for row in rows
        if socket.ntohs(row.dwLocalPort & 0xFFFF) == port
    ]


# --- Pastas do usuário ----------------------------------------------------------

def _enable_privilege(name: str) -> bool:
    token = HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(token)
    ):
        return False
    try:
        privileges = TOKEN_PRIVILEGES()
        privileges.PrivilegeCount = 1
        privileges.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(privileges.Privileges[0].Luid)):
            return False
        ctypes.set_last_error(0)
        if not advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(privileges), 0, None, None):
            return False
        return ctypes.get_last_error() == 0
    finally:
        kernel32.CloseHandle(token)


def _open_shell_process(access: int) -> int:
    """Handle do processo do Explorer — o usuário que está de fato na área de trabalho."""
    window = user32.GetShellWindow()
    if not window:
        raise OSError("o Explorer do Windows não está em execução")
    pid = DWORD()
    user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
    handle = kernel32.OpenProcess(access, False, pid.value)
    if not handle and ctypes.get_last_error() == ERROR_ACCESS_DENIED:
        # O app foi elevado com a conta de outro usuário (administrador).
        if _enable_privilege("SeDebugPrivilege"):
            handle = kernel32.OpenProcess(access, False, pid.value)
    if not handle:
        raise _win_error("OpenProcess(explorer)")
    return handle


def _shell_token(process: int, access: int) -> int:
    token = HANDLE()
    if not advapi32.OpenProcessToken(process, access, ctypes.byref(token)):
        raise _win_error("OpenProcessToken")
    return token.value


def _known_folder(folder: GUID, token: int | None) -> str:
    pointer = LPVOID()
    result = shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, token, ctypes.byref(pointer))
    try:
        if result != 0 or not pointer.value:
            raise OSError(f"SHGetKnownFolderPath falhou (0x{result & 0xFFFFFFFF:08X})")
        return ctypes.wstring_at(pointer.value)
    finally:
        ole32.CoTaskMemFree(pointer)


def system_directory() -> str:
    """C:\\Windows\\System32, perguntado ao Windows (variáveis de ambiente podem ser adulteradas)."""
    buffer = ctypes.create_unicode_buffer(260)
    if not kernel32.GetSystemDirectoryW(buffer, len(buffer)):
        raise _win_error("GetSystemDirectory")
    return buffer.value


def program_files() -> str:
    return _known_folder(FOLDERID_PROGRAM_FILES, None)


def _sid_string(sid_pointer) -> str:
    text = LPVOID()
    if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
        raise _win_error("ConvertSidToStringSid")
    try:
        return ctypes.wstring_at(text.value)
    finally:
        kernel32.LocalFree(text)


def process_user_sid() -> str:
    """SID da conta com que este processo está rodando."""
    token = HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise _win_error("OpenProcessToken")
    try:
        size = DWORD(0)
        advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, buffer, size, ctypes.byref(size)):
            raise _win_error("GetTokenInformation")
        # TOKEN_USER começa com o ponteiro para o SID.
        return _sid_string(ctypes.cast(buffer, ctypes.POINTER(LPVOID)).contents)
    finally:
        kernel32.CloseHandle(token)


def session_user() -> tuple[str, str]:
    """(domínio, usuário) de quem está logado na área de trabalho onde o app aparece."""

    def query(info_class: int) -> str:
        pointer = LPVOID()
        size = DWORD()
        if not wtsapi32.WTSQuerySessionInformationW(
            None, WTS_CURRENT_SESSION, info_class, ctypes.byref(pointer), ctypes.byref(size)
        ):
            raise _win_error("WTSQuerySessionInformation")
        try:
            return ctypes.wstring_at(pointer.value)
        finally:
            wtsapi32.WTSFreeMemory(pointer)

    return query(WTS_DOMAIN_NAME), query(WTS_USER_NAME)


def account_sid(domain: str, user: str) -> str:
    account = f"{domain}\\{user}" if domain else user
    sid_size, domain_size, use = DWORD(0), DWORD(0), ctypes.c_int(0)
    advapi32.LookupAccountNameW(
        None, account, None, ctypes.byref(sid_size), None, ctypes.byref(domain_size), ctypes.byref(use)
    )
    if not sid_size.value:
        raise _win_error("LookupAccountName")
    sid = ctypes.create_string_buffer(sid_size.value)
    referenced = ctypes.create_unicode_buffer(max(domain_size.value, 1))
    if not advapi32.LookupAccountNameW(
        None, account, sid, ctypes.byref(sid_size), referenced, ctypes.byref(domain_size), ctypes.byref(use)
    ):
        raise _win_error("LookupAccountName")
    return _sid_string(sid)


def profile_dirs_from_registry(sid: str) -> tuple[str, str]:
    """(LocalAppData, pasta de perfil) de uma conta, lidos do Registro pelo SID."""
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\{sid}"
    ) as key:
        profile = winreg.ExpandEnvironmentStrings(winreg.QueryValueEx(key, "ProfileImagePath")[0])
    local = None
    try:
        with winreg.OpenKey(
            winreg.HKEY_USERS, rf"{sid}\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
        ) as key:
            local = winreg.QueryValueEx(key, "Local AppData")[0]
    except OSError:
        pass
    if not local or not os.path.isdir(local):
        local = os.path.join(profile, "AppData", "Local")
    return local, profile


class _DesktopUser:
    """Quem está usando o computador — que pode não ser a conta do processo.

    Se o UAC foi respondido com a senha de outra conta (ou com a "Proteção de
    administrador" do Windows 11, que eleva sempre em uma conta separada), o
    processo roda com outro perfil: o Discord e as chaves SSH continuam no
    perfil de quem está na área de trabalho.
    """

    def __init__(self) -> None:
        self.other_account = False
        self.domain = self.name = ""
        self.sid: str | None = None
        local = profile = None
        try:
            self.domain, self.name = session_user()
            self.sid = account_sid(self.domain, self.name)
            if self.sid != process_user_sid():
                self.other_account = True
                local, profile = profile_dirs_from_registry(self.sid)
        except OSError as exc:
            log.info("usuário da área de trabalho não identificado: %s", exc)
            self.other_account = False
        if self.sid is None:
            try:
                self.sid = process_user_sid()
            except OSError:
                pass
        if local is None or profile is None:
            try:
                local = _known_folder(FOLDERID_LOCAL_APP_DATA, None)
                profile = _known_folder(FOLDERID_PROFILE, None)
            except OSError:
                local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
                profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        self.local_app_data, self.profile = local, profile

    def environment_overrides(self) -> dict[str, str]:
        """Variáveis que precisam apontar para o perfil dele em um processo iniciado em seu nome."""
        drive, tail = os.path.splitdrive(self.profile)
        temp = os.path.join(self.local_app_data, "Temp")
        return {
            "USERPROFILE": self.profile,
            "HOMEDRIVE": drive,
            "HOMEPATH": tail or "\\",
            "APPDATA": os.path.join(self.profile, "AppData", "Roaming"),
            "LOCALAPPDATA": self.local_app_data,
            "TEMP": temp,
            "TMP": temp,
            "USERNAME": self.name,
            "USERDOMAIN": self.domain,
        }


_desktop_user: _DesktopUser | None = None


def desktop_user() -> _DesktopUser:
    global _desktop_user
    if _desktop_user is None:
        _desktop_user = _DesktopUser()
    return _desktop_user


def user_dirs() -> tuple[str, str]:
    """(LocalAppData, pasta de perfil) do usuário da área de trabalho."""
    user = desktop_user()
    return user.local_app_data, user.profile


def desktop_user_is_other_account() -> bool:
    """True se o app roda com uma conta diferente da que está na área de trabalho."""
    return desktop_user().other_account


# --- Iniciar processos ------------------------------------------------------------

_spawn_lock = threading.Lock()


def clean_environment() -> dict[str, str]:
    """Ambiente sem as variáveis que o empacotador (PyInstaller) acrescenta."""
    blocked_prefixes = ("_PYI", "_MEI")
    blocked = {"TCL_LIBRARY", "TK_LIBRARY", "TIX_LIBRARY"}
    return {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(blocked_prefixes) and key.upper() not in blocked
    }


class default_dll_directory:
    """O executável empacotado altera a busca de DLLs do processo, e isso é
    herdado pelos processos filhos. Restaura o padrão enquanto um filho é criado."""

    def __enter__(self):
        _spawn_lock.acquire()
        self._frozen_dir = getattr(sys, "_MEIPASS", None)
        if self._frozen_dir:
            kernel32.SetDllDirectoryW(None)

    def __exit__(self, *exc):
        if self._frozen_dir:
            kernel32.SetDllDirectoryW(self._frozen_dir)
        _spawn_lock.release()


# --- Permissões dos objetos do processo novo --------------------------------------
#
# O Windows protege o processo novo (o objeto do processo, a thread e o token
# dele) com o DACL padrão de QUEM O CRIA — não do usuário com que ele vai rodar.
# Com o launcher elevado, esse DACL é "só Administradores e SYSTEM". O Discord,
# rodando como usuário comum, não consegue abrir o próprio token, e o Chromium
# dele falha ao criar os processos filhos ("'GPU' process exited with
# 'launch-failed'"): o Discord fecha assim que abre. Por isso, enquanto o
# processo é criado, o DACL padrão do launcher passa a ser o do usuário comum.


def _token_default_dacl(token) -> ctypes.Array:
    """DACL padrão do token, dentro de um buffer que começa com um TOKEN_DEFAULT_DACL
    (o buffer precisa continuar vivo enquanto o ponteiro para o ACL for usado)."""
    size = DWORD(0)
    advapi32.GetTokenInformation(token, TOKEN_DEFAULT_DACL_CLASS, None, 0, ctypes.byref(size))
    if not size.value:
        raise _win_error("GetTokenInformation(TokenDefaultDacl)")
    buffer = ctypes.create_string_buffer(size.value)
    if not advapi32.GetTokenInformation(token, TOKEN_DEFAULT_DACL_CLASS, buffer, size, ctypes.byref(size)):
        raise _win_error("GetTokenInformation(TokenDefaultDacl)")
    return buffer


def _own_logon_sid() -> str | None:
    """SID da sessão de logon deste processo (o mesmo da área de trabalho, quando a conta é a mesma)."""
    token = HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        size = DWORD(0)
        advapi32.GetTokenInformation(token, TOKEN_LOGON_SID_CLASS, None, 0, ctypes.byref(size))
        if size.value < ctypes.sizeof(TOKEN_GROUPS_1):
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_LOGON_SID_CLASS, buffer, size, ctypes.byref(size)):
            return None
        groups = TOKEN_GROUPS_1.from_buffer(buffer)
        if groups.GroupCount < 1 or not groups.Groups[0].Sid:
            return None
        return _sid_string(groups.Groups[0].Sid)
    except OSError:
        return None
    finally:
        kernel32.CloseHandle(token)


def _set_token_default_dacl(token, acl) -> None:
    holder = TOKEN_DEFAULT_DACL(acl)
    if not advapi32.SetTokenInformation(token, TOKEN_DEFAULT_DACL_CLASS, ctypes.byref(holder), ctypes.sizeof(holder)):
        raise _win_error("SetTokenInformation(TokenDefaultDacl)")


class _UserDacl:
    """Um ACL como o DACL padrão de um usuário comum: acesso total ao usuário e ao
    SYSTEM e leitura/execução à sessão de logon. Com `admins`, também acesso total
    aos Administradores (o launcher elevado com outra conta precisa disso para
    encerrar o processo depois)."""

    def __init__(self, user_sid: str, logon_sid: str | None = None, admins: bool = True):
        self._descriptor = LPVOID()
        aces = [f"(A;;GA;;;{user_sid})", "(A;;GA;;;SY)"]
        if logon_sid:
            aces.append(f"(A;;GRGX;;;{logon_sid})")
        if admins:
            aces.append("(A;;GA;;;BA)")
        sddl = "D:" + "".join(aces)
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, SDDL_REVISION_1, ctypes.byref(self._descriptor), None
        ):
            raise _win_error("ConvertStringSecurityDescriptorToSecurityDescriptor")
        present, defaulted, acl = BOOL(), BOOL(), LPVOID()
        if not advapi32.GetSecurityDescriptorDacl(
            self._descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)
        ) or not present.value or not acl.value:
            self.close()
            raise OSError("não foi possível montar o DACL do usuário")
        self.acl = acl.value

    def close(self) -> None:
        if self._descriptor.value:
            kernel32.LocalFree(self._descriptor)
            self._descriptor = LPVOID()


class _creator_default_dacl:
    """Troca o DACL padrão deste processo enquanto um processo filho é criado."""

    def __init__(self, acl):
        self._acl = acl

    def __enter__(self):
        token = HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), TOKEN_ADJUST_DEFAULT | TOKEN_QUERY, ctypes.byref(token)
        ):
            raise _win_error("OpenProcessToken")
        try:
            self._saved = _token_default_dacl(token)
            _set_token_default_dacl(token, self._acl)
        except BaseException:
            kernel32.CloseHandle(token)
            raise
        self._token = token
        return self

    def __exit__(self, *exc):
        try:
            saved = TOKEN_DEFAULT_DACL.from_buffer(self._saved).DefaultDacl
            _set_token_default_dacl(self._token, saved)
        except OSError as error:
            log.warning("não foi possível restaurar o DACL padrão: %s", error)
        finally:
            kernel32.CloseHandle(self._token)


def spawn_as_desktop_user(executable: str, arguments: list[str]) -> int:
    """Inicia o programa como filho do Explorer; devolve o PID.

    O processo herda o token do Explorer, então roda como o usuário da área de
    trabalho e sem privilégio de administrador, mesmo que o launcher esteja elevado.
    """
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline([executable, *arguments]))
    parent = HANDLE(_open_shell_process(PROCESS_CREATE_PROCESS | PROCESS_QUERY_LIMITED_INFORMATION))
    environment = LPVOID()
    attributes = None
    explorer_dacl = None        # o DACL padrão do próprio Explorer: o mais fiel ao "abrir pelo atalho"
    user_dacl = None
    try:
        try:
            token = _shell_token(parent, TOKEN_QUERY | TOKEN_DUPLICATE)
        except OSError:
            token = None
        if token:
            try:
                if not userenv.CreateEnvironmentBlock(ctypes.byref(environment), token, False):
                    environment = LPVOID()
                try:
                    explorer_dacl = _token_default_dacl(token)
                    if not TOKEN_DEFAULT_DACL.from_buffer(explorer_dacl).DefaultDacl:
                        explorer_dacl = None
                except OSError:
                    explorer_dacl = None
            finally:
                kernel32.CloseHandle(token)
        fallback_environment = None
        if not environment.value:
            fallback_environment = _hand_built_environment()
        if explorer_dacl is not None:
            child_acl = TOKEN_DEFAULT_DACL.from_buffer(explorer_dacl).DefaultDacl
        else:
            user_dacl = _UserDacl(_desktop_user_sid())
            child_acl = user_dacl.acl

        size = SIZE_T(0)
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attributes = ctypes.create_string_buffer(size.value)
        if not kernel32.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
            attributes = None
            raise _win_error("InitializeProcThreadAttributeList")
        if not kernel32.UpdateProcThreadAttribute(
            attributes, 0, PROC_THREAD_ATTRIBUTE_PARENT_PROCESS,
            ctypes.byref(parent), ctypes.sizeof(parent), None, None,
        ):
            raise _win_error("UpdateProcThreadAttribute")

        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        startup.lpAttributeList = ctypes.cast(attributes, LPVOID)
        info = PROCESS_INFORMATION()
        with default_dll_directory(), _creator_default_dacl(child_acl):
            created = kernel32.CreateProcessW(
                executable, command_line, None, None, False,
                EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                environment if environment.value else fallback_environment,
                os.path.dirname(executable) or None,
                ctypes.byref(startup), ctypes.byref(info),
            )
            error = None if created else _win_error("CreateProcess")
        if error is not None:
            raise error
        kernel32.CloseHandle(info.hThread)
        kernel32.CloseHandle(info.hProcess)
        return info.dwProcessId
    finally:
        if attributes is not None:
            kernel32.DeleteProcThreadAttributeList(attributes)
        if environment.value:
            userenv.DestroyEnvironmentBlock(environment)
        if user_dacl is not None:
            user_dacl.close()
        kernel32.CloseHandle(parent)


def _desktop_user_sid() -> str:
    sid = desktop_user().sid
    if not sid:
        raise OSError("usuário da área de trabalho não identificado")
    return sid


def _hand_built_environment():
    """Bloco de ambiente montado à mão: o do launcher, sem as variáveis do
    empacotador e, se o launcher foi elevado com outra conta, com as variáveis
    de perfil apontando para o usuário da área de trabalho."""
    variables = clean_environment()
    if desktop_user().other_account:
        variables.update(desktop_user().environment_overrides())
    ordered = sorted(variables.items(), key=lambda item: item[0].upper())
    block = "".join(f"{key}={value}\0" for key, value in ordered) + "\0"
    return ctypes.create_unicode_buffer(block)      # o ctypes calcula o tamanho em UTF-16


def _limited_token() -> int:
    """Cópia do token deste processo sem os poderes de administrador.

    É o mesmo tipo de token que o UAC entrega aos programas comuns: grupo
    Administradores só para negação, privilégios removidos, integridade média.
    """
    own = HANDLE()
    access = TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY | TOKEN_QUERY | TOKEN_ADJUST_DEFAULT
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), access, ctypes.byref(own)):
        raise _win_error("OpenProcessToken")
    limited = HANDLE()
    try:
        if not advapi32.CreateRestrictedToken(own, LUA_TOKEN, 0, None, 0, None, 0, None, ctypes.byref(limited)):
            raise _win_error("CreateRestrictedToken")
    finally:
        kernel32.CloseHandle(own)
    integrity = LPVOID()
    try:
        if not advapi32.ConvertStringSidToSidW(MEDIUM_INTEGRITY_SID, ctypes.byref(integrity)):
            raise _win_error("ConvertStringSidToSid")
        label = TOKEN_MANDATORY_LABEL()
        label.Label.Sid = integrity.value
        label.Label.Attributes = SE_GROUP_INTEGRITY
        size = ctypes.sizeof(label) + advapi32.GetLengthSid(integrity)
        if not advapi32.SetTokenInformation(limited, TOKEN_INTEGRITY_LEVEL_CLASS, ctypes.byref(label), size):
            raise _win_error("SetTokenInformation")
    except OSError:
        kernel32.CloseHandle(limited)
        raise
    finally:
        if integrity.value:
            kernel32.LocalFree(integrity)
    return limited.value


def spawn_with_limited_token(executable: str, arguments: list[str]) -> int:
    """Inicia o programa com o token "rebaixado" do próprio launcher; devolve o PID.

    Não depende do Explorer. Só faz sentido quando o launcher roda com a conta
    de quem está na área de trabalho (com outra conta, o programa abriria no
    perfil errado).
    """
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline([executable, *arguments]))
    # Só é usado com a conta da área de trabalho: o DACL fica igual ao de um usuário
    # comum que abre o programa (o launcher, com o mesmo usuário, ainda consegue encerrá-lo).
    user_dacl = _UserDacl(process_user_sid(), logon_sid=_own_logon_sid(), admins=False)
    environment = LPVOID()
    token = None
    try:
        token = HANDLE(_limited_token())
        # O token rebaixado herda o DACL padrão do token elevado ("só Administradores
        # e SYSTEM"), e os objetos que o programa criar ficariam inacessíveis para ele mesmo.
        _set_token_default_dacl(token, user_dacl.acl)
        if not userenv.CreateEnvironmentBlock(ctypes.byref(environment), token, False):
            environment = LPVOID()
        fallback_environment = None if environment.value else _hand_built_environment()
        startup = STARTUPINFOW()
        startup.cb = ctypes.sizeof(STARTUPINFOW)
        info = PROCESS_INFORMATION()
        with default_dll_directory(), _creator_default_dacl(user_dacl.acl):
            created = advapi32.CreateProcessAsUserW(
                token, executable, command_line, None, None, False,
                CREATE_UNICODE_ENVIRONMENT | CREATE_NEW_PROCESS_GROUP,
                environment if environment.value else fallback_environment,
                os.path.dirname(executable) or None,
                ctypes.byref(startup), ctypes.byref(info),
            )
            error = None if created else _win_error("CreateProcessAsUser")
        if error is not None:
            raise error
        kernel32.CloseHandle(info.hThread)
        kernel32.CloseHandle(info.hProcess)
        return info.dwProcessId
    finally:
        if environment.value:
            userenv.DestroyEnvironmentBlock(environment)
        if token is not None:
            kernel32.CloseHandle(token)
        user_dacl.close()


_DE_ELEVATION_ATTEMPTS = 3


def spawn_detached(executable: str, arguments: list[str]) -> int:
    """Inicia um programa independente do launcher; devolve o PID.

    O programa nunca herda o privilégio de administrador do launcher. O Discord
    fica em uma pasta que qualquer programa do usuário consegue alterar;
    executá-la elevada daria poder de administrador a quem trocasse o arquivo.
    Com o launcher elevado, tenta-se, nesta ordem:

    1. iniciar como filho do Explorer (o usuário da área de trabalho);
    2. iniciar com o token rebaixado do próprio launcher (se a conta for a mesma).

    Se nada disso funcionar, falha — em vez de abrir como administrador.
    """
    if not is_elevated():
        with default_dll_directory():
            process = subprocess.Popen(
                [executable, *arguments],
                cwd=os.path.dirname(executable) or None,
                env=clean_environment(),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        return process.pid

    for attempt in range(_DE_ELEVATION_ATTEMPTS):
        try:
            return spawn_as_desktop_user(executable, arguments)
        except Exception as exc:
            log.warning("início como filho do Explorer falhou (tentativa %d): %s", attempt + 1, exc)
        if not desktop_user().other_account:
            try:
                return spawn_with_limited_token(executable, arguments)
            except Exception as exc:
                log.warning("início com token rebaixado falhou (tentativa %d): %s", attempt + 1, exc)
        if attempt + 1 < _DE_ELEVATION_ATTEMPTS:
            time.sleep(1.5)                 # o Explorer pode estar só reiniciando
    raise OSError("o Windows não deixou iniciá-lo sem privilégio de administrador")


_job = None


def kill_with_launcher(process: subprocess.Popen) -> None:
    """Garante que o processo filho morra junto com o launcher (mesmo em um crash)."""
    global _job
    try:
        if _job is None:
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                raise _win_error("CreateJobObject")
            limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                kernel32.CloseHandle(job)
                raise _win_error("SetInformationJobObject")
            _job = job
        if not kernel32.AssignProcessToJobObject(_job, int(process._handle)):
            raise _win_error("AssignProcessToJobObject")
    except OSError as exc:
        log.info("job object indisponível: %s", exc)
