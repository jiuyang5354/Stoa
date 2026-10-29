// Stoa.exe：无控制台，用同目录 python\pythonw.exe 跑 `-m stock_agent desktop`（独立窗口由 desktop.py 打开）
#include <windows.h>
#include <wchar.h>
int WINAPI wWinMain(HINSTANCE a, HINSTANCE b, PWSTR c, int d){
  wchar_t dir[MAX_PATH], py[MAX_PATH], cmd[MAX_PATH*2];
  GetModuleFileNameW(NULL, dir, MAX_PATH);
  wchar_t *p = wcsrchr(dir, L'\\'); if (p) *p = 0;
  SetEnvironmentVariableW(L"PYTHONUTF8", L"1"); SetEnvironmentVariableW(L"PYTHONIOENCODING", L"utf-8");
  swprintf(py, MAX_PATH, L"%ls\\python\\pythonw.exe", dir);
  if (GetFileAttributesW(py) == INVALID_FILE_ATTRIBUTES){
    MessageBoxW(NULL, L"找不到 python\\pythonw.exe，请完整解压压缩包后再运行。", L"Stoa", MB_ICONERROR); return 1; }
  swprintf(cmd, MAX_PATH*2, L"\"%ls\" -m stock_agent desktop", py);
  STARTUPINFOW si = {sizeof si}; PROCESS_INFORMATION pi;
  if (!CreateProcessW(py, cmd, NULL, NULL, FALSE, 0, NULL, dir, &si, &pi)){
    MessageBoxW(NULL, L"启动失败", L"Stoa", MB_ICONERROR); return 1; }
  CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
  return 0;
}
