// 卸载 Stoa.exe：停掉本文件夹里正在运行的 Stoa → 询问是否删除数据（%LOCALAPPDATA%\Stoa）→ 删除整个程序文件夹
#include <windows.h>
#include <tlhelp32.h>
#include <shellapi.h>
#include <wchar.h>

static wchar_t dir[MAX_PATH];

static void kill_ours(void){
  DWORD me = GetCurrentProcessId();
  size_t n = wcslen(dir);
  HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
  if (snap == INVALID_HANDLE_VALUE) return;
  PROCESSENTRY32W pe = {sizeof pe};
  for (BOOL ok = Process32FirstW(snap, &pe); ok; ok = Process32NextW(snap, &pe)){
    if (pe.th32ProcessID == me) continue;
    HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE | SYNCHRONIZE, FALSE, pe.th32ProcessID);
    if (!h) continue;
    wchar_t path[MAX_PATH]; DWORD len = MAX_PATH;
    if (QueryFullProcessImageNameW(h, 0, path, &len) && len > n && _wcsnicmp(path, dir, n) == 0 && path[n] == L'\\'){
      TerminateProcess(h, 0); WaitForSingleObject(h, 3000);
    }
    CloseHandle(h);
  }
  CloseHandle(snap);
}

static void rmtree(const wchar_t *p){
  wchar_t from[MAX_PATH + 2] = {0};           // SHFileOperation 要求双 \0 结尾
  wcsncpy(from, p, MAX_PATH);
  SHFILEOPSTRUCTW op = {0};
  op.wFunc = FO_DELETE; op.pFrom = from;
  op.fFlags = FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI;
  SHFileOperationW(&op);
}

int WINAPI wWinMain(HINSTANCE a, HINSTANCE b, PWSTR c, int d){
  GetModuleFileNameW(NULL, dir, MAX_PATH);
  wchar_t *s = wcsrchr(dir, L'\\'); if (s) *s = 0;

  wchar_t data[MAX_PATH] = L"";
  DWORD m = GetEnvironmentVariableW(L"LOCALAPPDATA", data, MAX_PATH);
  if (m && m < MAX_PATH - 8) wcscat(data, L"\\Stoa"); else data[0] = 0;

  // 静默卸载（CI 自检用）：/S 保留数据，/S /purge 连数据一起删
  int quiet = c && wcsstr(c, L"/S") != NULL, purge = c && wcsstr(c, L"/purge") != NULL;
  wchar_t msg[MAX_PATH * 2 + 200];
  swprintf(msg, sizeof msg / sizeof *msg, L"要卸载 Stoa 吗？\n\n会先关闭正在运行的 Stoa，然后删除程序文件夹：\n%ls", dir);
  if (!quiet && MessageBoxW(NULL, msg, L"卸载 Stoa", MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2) != IDYES) return 0;

  int wipe = quiet ? (purge ? IDYES : IDNO) : IDNO;
  if (!quiet && data[0] && GetFileAttributesW(data) != INVALID_FILE_ATTRIBUTES){
    swprintf(msg, sizeof msg / sizeof *msg, L"是否同时删除你的数据？\n\n记忆、判断账本、自选股和大模型密钥都在：\n%ls\n\n选「否」会保留，下次重新解压 Stoa 还能接着用。", data);
    wipe = MessageBoxW(NULL, msg, L"卸载 Stoa", MB_YESNOCANCEL | MB_ICONWARNING | MB_DEFBUTTON2);
    if (wipe == IDCANCEL) return 0;
  }

  kill_ours();
  if (wipe == IDYES) rmtree(data);

  // 自己正在运行删不掉自己：交给一个延迟 2 秒的 cmd 去删整个文件夹
  wchar_t sys[MAX_PATH], cmd[MAX_PATH * 3];
  GetSystemDirectoryW(sys, MAX_PATH);
  swprintf(cmd, sizeof cmd / sizeof *cmd, L"\"%ls\\cmd.exe\" /c ping -n 3 127.0.0.1 >nul & rmdir /s /q \"%ls\"", sys, dir);
  STARTUPINFOW si = {sizeof si}; PROCESS_INFORMATION pi;
  wchar_t tmp[MAX_PATH]; GetTempPathW(MAX_PATH, tmp);
  if (CreateProcessW(NULL, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, tmp, &si, &pi)){
    CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
  }
  if (!quiet) MessageBoxW(NULL, wipe == IDYES ? L"Stoa 已卸载，数据也已删除。" : L"Stoa 已卸载。你的数据保留在 %LOCALAPPDATA%\\Stoa。", L"卸载 Stoa", MB_ICONINFORMATION);
  return 0;
}
