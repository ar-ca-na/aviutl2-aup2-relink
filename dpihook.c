/* WM_DPICHANGED を受けて、Windows が示す位置と大きさへ窓を合わせる。
   Tk はこの通知を処理しないので、拡大率の違うモニタへ移っても窓が前の大きさのまま残る。
   Python の ctypes コールバックで窓プロシージャを差し替えると tkinter と衝突して落ちるため、C で持つ。
   build: gcc -shared -O2 -s -o dpihook.dll dpihook.c -luser32 */
#include <windows.h>

static WNDPROC g_old;

static LRESULT CALLBACK proc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    if (m == WM_DPICHANGED) {
        const RECT *r = (const RECT *)l;
        SetWindowPos(h, NULL, r->left, r->top, r->right - r->left, r->bottom - r->top,
                     SWP_NOZORDER | SWP_NOACTIVATE);
        return 0;
    }
    return CallWindowProcW(g_old, h, m, w, l);
}

__declspec(dllexport) int hook_dpi_changed(HWND h)
{
    g_old = (WNDPROC)SetWindowLongPtrW(h, GWLP_WNDPROC, (LONG_PTR)proc);
    return g_old != NULL;
}
