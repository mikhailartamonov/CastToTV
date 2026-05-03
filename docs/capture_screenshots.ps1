# Helper: capture CastToTV GUI window screenshots into docs/images/.
# Usage: pwsh docs/capture_screenshots.ps1 [main|all]
# Run from CastToTV repo root.

param(
    [string]$Mode = "main",
    [int]$WaitSec = 6,
    [string]$Out = "main.png"
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Drawing;
public class WinUtil {
    [DllImport("user32.dll")] public static extern IntPtr FindWindow(string cls, string title);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
    [DllImport("user32.dll")] public static extern IntPtr FindWindowEx(IntPtr parent, IntPtr child, string cls, string title);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
}
"@

function Find-Window([string]$titleSubstring) {
    foreach ($p in (Get-Process | Where-Object { $_.MainWindowTitle -like "*$titleSubstring*" })) {
        return $p.MainWindowHandle
    }
    return [IntPtr]::Zero
}

function Capture-Window([IntPtr]$hwnd, [string]$outPath) {
    [WinUtil+RECT]$r = New-Object WinUtil+RECT
    [void][WinUtil]::GetWindowRect($hwnd, [ref]$r)
    $w = $r.R - $r.L; $h = $r.B - $r.T
    if ($w -le 0 -or $h -le 0) { throw "Window has zero size — not visible yet" }
    [void][WinUtil]::SetForegroundWindow($hwnd)
    Start-Sleep -Milliseconds 250
    $bmp = New-Object System.Drawing.Bitmap $w, $h
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($r.L, $r.T, 0, 0, [System.Drawing.Size]::new($w, $h))
    $bmp.Save($outPath, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
    Write-Host "Saved $outPath ($w x $h)"
}

$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$imgDir = Join-Path $repo "docs/images"
New-Item -ItemType Directory -Force -Path $imgDir | Out-Null

# Launch GUI
$proc = Start-Process -FilePath "python" -ArgumentList "cast_to_tv.py" -WorkingDirectory $repo -PassThru
Start-Sleep -Seconds $WaitSec

try {
    $hwnd = Find-Window "Caster"
    if ($hwnd -eq [IntPtr]::Zero) {
        for ($i = 0; $i -lt 10; $i++) {
            Start-Sleep -Milliseconds 500
            $hwnd = Find-Window "Caster"
            if ($hwnd -ne [IntPtr]::Zero) { break }
        }
    }
    if ($hwnd -eq [IntPtr]::Zero) { throw "Window not found after 8s" }

    Capture-Window $hwnd (Join-Path $imgDir $Out)

    if ($Mode -eq "all") {
        Write-Host "Manual capture mode: GUI is up."
        Write-Host "  - Click FIND DLNA, wait for device list, then run: pwsh -c '. .\docs\capture_screenshots.ps1; Capture-Window (Find-Window Caster) docs/images/devices.png'"
        Write-Host "  - Cast a video to TV, then capture cast-in-progress with seek bar -> cast.png"
        Write-Host "  - Click DONGLE WiFi -> capture dialog -> dongle.png"
        Write-Host "Press Enter to close GUI..."
        [void](Read-Host)
    }
} finally {
    if (-not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
}
