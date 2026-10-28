// Stoa.exe：用同目录 python\python.exe 跑 `-m stock_agent desktop`，共用这个控制台，关窗即退出
#include <windows.h>
#include <wchar.h>
int wmain(void){
  wchar_t dir[MAX_PATH], py[MAX_PATH], cmd[MAX_PATH*2];
  GetModuleFileNameW(NULL, dir, MAX_PATH);
  wchar_t *p = wcsrchr(dir, L'\\'); if (p) *p = 0;
  SetConsoleOutputCP(65001); SetConsoleTitleW(L"Stoa \x00B7 \x80A1\x5E02 Agent");
  SetEnvironmentVariableW(L"PYTHONUTF8", L"1"); SetEnvironmentVariableW(L"PYTHONIOENCODING", L"utf-8");
  SetEnvironmentVariableW(L"PYTHONDONTWRITEBYTECODE", NULL);
  swprintf(py, MAX_PATH, L"%ls\\python\\python.exe", dir);
  if (GetFileAttributesW(py) == INVALID_FILE_ATTRIBUTES){
    MessageBoxW(NULL, L"\x627E\x4E0D\x5230 python\\python.exe\xFF0C\x8BF7\x5B8C\x6574\x89E3\x538B\x538B\x7F29\x5305\x540E\x518D\x8FD0\x884C\x3002", L"Stoa", MB_ICONERROR); return 1; }
  swprintf(cmd, MAX_PATH*2, L"\"%ls\" -m stock_agent desktop", py);
  STARTUPINFOW si = {sizeof si}; PROCESS_INFORMATION pi;
  if (!CreateProcessW(py, cmd, NULL, NULL, FALSE, 0, NULL, dir, &si, &pi)){
    MessageBoxW(NULL, L"\x542F\x52A8\x5931\x8D25", L"Stoa", MB_ICONERROR); return 1; }
  WaitForSingleObject(pi.hProcess, INFINITE);
  DWORD code = 0; GetExitCodeProcess(pi.hProcess, &code);
  if (code){ wprintf(L"\n\x9000\x51FA\x7801 %lu\xFF0C\x6309\x56DE\x8F66\x5173\x95ED\x2026", code); getwchar(); }
  return (int)code;
}
