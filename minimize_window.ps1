# 最小化浏览器 CDP 窗口（会话保持后台运行；不要关闭窗口，关闭会导致接口 503）
# 用法：powershell -ExecutionPolicy Bypass -File minimize_window.ps1
$sig = @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class Win {
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder sb, int max);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lp);
}
"@
try { Add-Type -TypeDefinition $sig -ErrorAction Stop } catch { Write-Host "编译 Win32 帮助类失败: $($_.Exception.Message)"; exit 1 }

$script:found = @()
$cb = [Win+EnumWindowsProc]{
    param($h, $l)
    $sb = New-Object System.Text.StringBuilder 256
    [void][Win]::GetWindowText($h, $sb, 256)
    $t = $sb.ToString()
    if ($t -and ($t -like "*Edge*" -or $t -like "*Chrome*") -and [Win]::IsWindowVisible($h)) {
        $script:found += $h
    }
    return $true
}
[void][Win]::EnumWindows($cb, [IntPtr]::Zero)
foreach ($h in $script:found) { [void][Win]::ShowWindow($h, 6) }
Write-Host "已最小化 $($script:found.Count) 个浏览器窗口"
Write-Host "注意：不要关闭窗口，关闭会导致接口返回 503"