# Interactive capture flow:
#   1. Launches GUI.
#   2. Pauses 14 s — USER clicks DONGLE WiFi button → dongle.png captured.
#   3. Pauses 5 s — USER closes the dongle dialog.
#   4. Pauses 28 s — USER clicks [...] to pick a video, picks one, clicks <<< CAST >>>,
#      and waits for the TV to start playing.
#   5. cast.png captured, GUI closed.
# Run from CastToTV repo root.

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W {
    [DllImport("user32.dll")] public static extern IntPtr FindWindow(string c, string t);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
}
"@

function Find-WindowHandle([string]$titleSubstr) {
    foreach ($p in (Get-Process -ErrorAction SilentlyContinue |
                    Where-Object { $_.MainWindowTitle -like "*$titleSubstr*" })) {
        return $p.MainWindowHandle
    }
    return [IntPtr]::Zero
}

function Capture-Region([int]$l, [int]$t, [int]$w, [int]$h, [string]$out) {
    if ($w -le 0 -or $h -le 0) { Write-Host "ERROR: zero size for $out"; return }
    $bmp = New-Object System.Drawing.Bitmap $w, $h
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($l, $t, 0, 0, [System.Drawing.Size]::new($w, $h))
    $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
    Write-Host "saved $out ($w x $h)"
}

$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$imgDir = Join-Path $repo "docs/images"

Write-Host "=== STEP 1: launching GUI ===" -ForegroundColor Cyan
$proc = Start-Process -FilePath "python" -ArgumentList "cast_to_tv.py" -WorkingDirectory $repo -PassThru
Start-Sleep -Seconds 6

$hwnd = Find-WindowHandle "Caster"
if ($hwnd -eq [IntPtr]::Zero) {
    for ($i = 0; $i -lt 10; $i++) { Start-Sleep 0.5; $hwnd = Find-WindowHandle "Caster"; if ($hwnd -ne [IntPtr]::Zero) { break } }
}
if ($hwnd -eq [IntPtr]::Zero) { Write-Host "ERROR: window not found"; $proc.Kill(); exit 1 }

[W+RECT]$r = New-Object W+RECT
[void][W]::GetWindowRect($hwnd, [ref]$r)
$pad = 80   # widen so dongle dialog (which may sit outside main window) is captured

try {
    Write-Host ""
    Write-Host ">>> NOW click 'DONGLE WiFi' button — capturing in 14 seconds <<<" -ForegroundColor Yellow
    1..14 | ForEach-Object { Start-Sleep 1; Write-Host -NoNewline "$($_) " }
    Write-Host ""

    [void][W]::GetWindowRect($hwnd, [ref]$r)
    $left   = [Math]::Max(0, $r.L - $pad)
    $top    = [Math]::Max(0, $r.T - 50)
    $width  = ($r.R + $pad) - $left
    $height = ($r.B + 50)  - $top
    Capture-Region $left $top $width $height (Join-Path $imgDir "dongle.png")

    Write-Host ""
    Write-Host ">>> close the dongle dialog. Then click [...] next to FILE, pick a video, click <<< CAST >>>." -ForegroundColor Yellow
    Write-Host ">>> Capture in 28 seconds — give the TV time to start playback. <<<" -ForegroundColor Yellow
    1..28 | ForEach-Object { Start-Sleep 1; Write-Host -NoNewline "$($_) " }
    Write-Host ""

    $hwnd2 = Find-WindowHandle "Caster"
    if ($hwnd2 -eq [IntPtr]::Zero) { $hwnd2 = $hwnd }
    [void][W]::GetWindowRect($hwnd2, [ref]$r)
    [void][W]::SetForegroundWindow($hwnd2)
    Start-Sleep -Milliseconds 300
    $w2 = $r.R - $r.L; $h2 = $r.B - $r.T
    Capture-Region $r.L $r.T $w2 $h2 (Join-Path $imgDir "cast.png")
} finally {
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
}

Write-Host ""
Write-Host "Done. Check docs/images/dongle.png and docs/images/cast.png." -ForegroundColor Green
