<#
.SYNOPSIS
    Part A of the psmux go/no-go spike (docs/research/2026-09-03-psmux-windows-spike.md).

.DESCRIPTION
    Runs probes A0-A10 against a psmux binary and writes a markdown results
    file. Every probe records its raw output: the spike doc requires the
    verdict be reproducible "and not a vibe", so nothing here reports a
    bare pass/fail without the evidence behind it.

    Each probe stands in for a specific caller in claudespace/backends/tmux_cli.py.
    [MUST] failures are no-go; [WANT] failures are tracked workarounds.
#>
[CmdletBinding()]
param(
    [string]$Bin = "psmux",
    [string]$Sock = "csspike",
    [Parameter(Mandatory = $true)][string]$OutFile,
    [string]$Python = "python"
)

$ErrorActionPreference = "Continue"
$US = [char]0x1f          # the unit separator tmux_cli.py splits -F output on
$script:Results = @()
$script:Pane = $null      # %N id; what tmux_cli.py actually passes
$script:NarrowPane = $null

function Px([string[]]$A) {
    # psmux on the spike's dedicated socket. Mirrors tmux_cli._socket_args().
    #
    # Takes ONE array on purpose. With [Parameter(ValueFromRemainingArguments)]
    # this becomes an advanced function, PowerShell adds its common parameters,
    # and a bare `-p` (capture-pane, set-option, show-options, display-message,
    # paste-buffer all need it) binds as an ambiguous prefix of -ProgressAction
    # / -PipelineVariable instead of reaching psmux. Splatting a plain array
    # keeps every flag - and `--` - an inert string.
    $out = & $Bin -L $Sock @A 2>&1 | Out-String
    return @{ Text = $out.TrimEnd(); Code = $LASTEXITCODE }
}

function Probe {
    param(
        [string]$Id,
        [ValidateSet("MUST", "WANT")][string]$Level,
        [string]$Caller,
        [string]$Desc,
        [scriptblock]$Body
    )
    Write-Host "-- $Id [$Level] $Desc"
    $ok = $false
    $raw = ""
    try {
        $r = & $Body
        $ok = [bool]$r.Ok
        $raw = [string]$r.Raw
    }
    catch {
        $ok = $false
        $raw = "EXCEPTION: $_"
    }
    $script:Results += [pscustomobject]@{
        Id = $Id; Level = $Level; Caller = $Caller; Desc = $Desc
        Ok = $ok; Raw = $raw
    }
    Write-Host ("   {0}" -f $(if ($ok) { "PASS" } else { "FAIL" }))
    foreach ($l in ($raw -split "`n")) { Write-Host "      | $l" }
}

# --- A0 ---------------------------------------------------------------------
# Validated with the REAL parse_version, not a lookalike regex: the point of
# A0 is whether claudespace's own version gate accepts psmux's string.
$rawVersion = (& $Bin -V 2>&1 | Out-String).Trim()
Probe "A0" "MUST" "tmux_cli.version / parse_version" "version string parses to >= (3,0)" {
    $py = @"
import sys
sys.path.insert(0, '.')
from claudespace.backends.tmux_cli import parse_version, MIN_TMUX_VERSION
raw = sys.argv[1]
try:
    v = parse_version(raw)
except Exception as exc:
    print('parse_version raised:', exc); sys.exit(1)
print('raw=%r parsed=%r floor=%r' % (raw, v, MIN_TMUX_VERSION))
sys.exit(0 if v >= MIN_TMUX_VERSION else 1)
"@
    $out = ($py | & $Python - $rawVersion 2>&1 | Out-String).TrimEnd()
    @{ Ok = ($LASTEXITCODE -eq 0); Raw = "psmux -V => $rawVersion`n$out" }
}

# --- A1 ---------------------------------------------------------------------
Probe "A1" "MUST" "new_session / has_session" "detached server, inspectable with no client" {
    $a = Px @('new-session', '-d', '-s', 's1', '-c', $PWD.Path)
    $b = Px @('has-session', '-t', 's1')
    $c = Px @('list-panes', '-t', 's1', '-F', "#{pane_id}")
    $first = ($c.Text -split "`n" | Where-Object { $_ -match '^%\d+' } | Select-Object -First 1)
    if ($first) { $script:Pane = $first.Trim() }
    $ok = ($b.Code -eq 0) -and ($null -ne $script:Pane)
    @{ Ok = $ok; Raw = "new-session: $($a.Text)`nhas-session rc=$($b.Code)`nlist-panes: $($c.Text)`nresolved pane id: $script:Pane" }
}

# --- A2 --- the crux: the exact thing zellij cannot do (zellij#4508) ---------
Probe "A2" "MUST" "capture_pane" "capture-pane -p -J while DETACHED" {
    Px @('send-keys', '-t', $script:Pane, '-l', '--', "echo spike-marker-123") | Out-Null
    Px @('send-keys', '-t', $script:Pane, 'Enter') | Out-Null
    Start-Sleep -Milliseconds 800
    $cap = Px @('capture-pane', '-p', '-J', '-t', $script:Pane)
    @{ Ok = ($cap.Text -match 'spike-marker-123'); Raw = $cap.Text }
}

# --- A3 ---------------------------------------------------------------------
Probe "A3" "WANT" "capture_pane (-J join)" "-J joins a soft-wrapped long line" {
    Px @('new-window', '-t', 's1', '-n', 'narrow') | Out-Null
    Px @('resize-window', '-t', 's1:narrow', '-x', '40', '-y', '10') | Out-Null
    $np = Px @('list-panes', '-t', 's1:narrow', '-F', "#{pane_id}")
    $script:NarrowPane = (($np.Text -split "`n" | Where-Object { $_ -match '^%\d+' } | Select-Object -First 1)).Trim()
    Px @('send-keys', '-t', $script:NarrowPane, '-l', '--', "printf 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789XYZ-END'") | Out-Null
    Px @('send-keys', '-t', $script:NarrowPane, 'Enter') | Out-Null
    Start-Sleep -Milliseconds 800
    $cap = Px @('capture-pane', '-p', '-J', '-t', $script:NarrowPane)
    # The join worked if the whole token survives on one physical line.
    $joined = $cap.Text -split "`n" | Where-Object { $_ -match 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789XYZ-END' }
    @{ Ok = ($null -ne $joined -and $joined.Count -ge 1); Raw = $cap.Text }
}

# --- A4 --- the identity model; what zellij/wezterm lack --------------------
Probe "A4" "MUST" "set_pane_option / show_pane_option" "per-pane @cs_* option round-trips" {
    $s = Px @('set-option', '-p', '-t', $script:Pane, '@cs_role', 'researcher')
    $g = Px @('show-options', '-p', '-v', '-t', $script:Pane, '@cs_role')
    @{ Ok = ($g.Text.Trim() -eq 'researcher'); Raw = "set rc=$($s.Code) $($s.Text)`nshow => '$($g.Text)'" }
}

# --- A5 ---------------------------------------------------------------------
Probe "A5" "MUST" "list_panes_all" "@cs_* interpolate in list-panes -a -F" {
    Px @('set-option', '-p', '-t', $script:Pane, '@cs_workspace', '/some/marker') | Out-Null
    $fmt = "#{pane_id}$US#{@cs_workspace}$US#{@cs_role}"
    $r = Px @('list-panes', '-a', '-F', $fmt)
    $hit = $r.Text -split "`n" | Where-Object { $_ -match [regex]::Escape("/some/marker") }
    $ok = ($null -ne $hit -and $hit.Count -ge 1) -and ($hit[0] -like "*researcher*")
    # Render the separator visibly; a raw 0x1f is invisible in the report.
    @{ Ok = $ok; Raw = ($r.Text -replace [regex]::Escape($US), '<US>') }
}

# --- A6 ---------------------------------------------------------------------
Probe "A6" "MUST" "send_keys_literal" "send-keys -l -- types a leading dash literally" {
    Px @('send-keys', '-t', $script:Pane, '-l', '--', "-not-a-flag typed literally") | Out-Null
    Start-Sleep -Milliseconds 500
    $cap = Px @('capture-pane', '-p', '-J', '-t', $script:Pane)
    @{ Ok = ($cap.Text -match '-not-a-flag typed literally'); Raw = $cap.Text }
}

# --- A7 --- no paste-buffer => the large-handoff truncation bug returns ------
Probe "A7" "MUST" "send_text_paste" "named buffer round-trips >2.5KB, paste-buffer -d -p" {
    $big = "HEAD-" + ("x" * 3000) + "-TAIL"
    $set = Px @('set-buffer', '-b', 'csb', '--', $big)
    $show = Px @('show-buffer', '-b', 'csb')
    $intact = ($show.Text.Length -ge 3010) -and $show.Text.StartsWith("HEAD-") -and $show.Text.TrimEnd().EndsWith("-TAIL")
    $paste = Px @('paste-buffer', '-d', '-p', '-b', 'csb', '-t', $script:Pane)
    $ok = $intact -and ($paste.Code -eq 0)
    $summary = "set rc=$($set.Code); show len=$($show.Text.Length) head='$($show.Text.Substring(0,[Math]::Min(12,$show.Text.Length)))' tail='$($show.Text.Substring([Math]::Max(0,$show.Text.Length-12)))'; paste rc=$($paste.Code) $($paste.Text)"
    @{ Ok = $ok; Raw = $summary }
}

# --- A8 ---------------------------------------------------------------------
Probe "A8" "WANT" "pane_dims / pane_border_title" "geometry reports real numbers" {
    $d = Px @('display-message', '-p', '-t', $script:Pane, "#{pane_width}x#{pane_height}")
    $t = Px @('select-pane', '-t', $script:Pane, '-T', 'researcher')
    @{ Ok = ($d.Text -match '^\d+x\d+$'); Raw = "dims => '$($d.Text)'`nselect-pane -T rc=$($t.Code) $($t.Text)" }
}

# --- A9 ---------------------------------------------------------------------
Probe "A9" "MUST" "split_window / new_window / kill_session" "structure ops" {
    $sp = Px @('split-window', '-t', 's1')
    $nw = Px @('new-window', '-t', 's1', '-n', 'extra')
    $sel = Px @('select-pane', '-t', $script:Pane)
    $selw = Px @('select-window', '-t', 's1:extra')
    $panes = Px @('list-panes', '-a', '-F', "#{pane_id}")
    $count = ($panes.Text -split "`n" | Where-Object { $_ -match '^%\d+' }).Count
    $ks = Px @('kill-session', '-t', 's1')
    $gone = Px @('has-session', '-t', 's1')
    $ok = ($sp.Code -eq 0) -and ($nw.Code -eq 0) -and ($ks.Code -eq 0) -and ($gone.Code -ne 0)
    @{ Ok = $ok
       Raw = "split rc=$($sp.Code); new-window rc=$($nw.Code); select-pane rc=$($sel.Code); select-window rc=$($selw.Code); panes=$count`nkill-session rc=$($ks.Code); has-session after kill rc=$($gone.Code) (non-zero expected)" }
}

# --- A10 --------------------------------------------------------------------
Probe "A10" "MUST" "AD8 dedicated socket" "-L namespace invisible to the default socket" {
    Px @('new-session', '-d', '-s', 'isolated') | Out-Null
    $mine = Px @('list-sessions')
    $default = (& $Bin list-sessions 2>&1 | Out-String).TrimEnd()
    $ok = ($mine.Text -match 'isolated') -and ($default -notmatch 'isolated')
    @{ Ok = $ok; Raw = "on -L $Sock =>`n$($mine.Text)`n`non default socket =>`n$default" }
}

# --- teardown ---------------------------------------------------------------
# Scoped kill only. psmux diverges from tmux here: a BARE `kill-server` tears
# down every socket, not just this one (docs/compatibility.md).
& $Bin -L $Sock kill-server 2>&1 | Out-Null

# --- report -----------------------------------------------------------------
$musts = $script:Results | Where-Object { $_.Level -eq "MUST" }
$mustFail = $musts | Where-Object { -not $_.Ok }
$wantFail = $script:Results | Where-Object { $_.Level -eq "WANT" -and -not $_.Ok }

$lines = @()
$lines += "### Part A — direct CLI probes"
$lines += ""
$lines += "| Probe | Level | Stands in for | Result |"
$lines += "| --- | --- | --- | --- |"
foreach ($r in $script:Results) {
    $mark = if ($r.Ok) { "PASS" } else { "**FAIL**" }
    $lines += "| $($r.Id) | $($r.Level) | ``$($r.Caller)`` | $mark |"
}
$lines += ""
$lines += "#### Raw output"
$lines += ""
foreach ($r in $script:Results) {
    $lines += "<details><summary><code>$($r.Id)</code> [$($r.Level)] $($r.Desc) — $(if ($r.Ok) { 'PASS' } else { 'FAIL' })</summary>"
    $lines += ""
    $lines += '```'
    $lines += $r.Raw
    $lines += '```'
    $lines += ""
    $lines += "</details>"
    $lines += ""
}

$partA = if ($mustFail.Count -eq 0 -and $wantFail.Count -eq 0) { "GO" }
         elseif ($mustFail.Count -eq 0) { "conditional GO" }
         else { "NO-GO" }
$lines += "**Part A verdict: $partA** — $($musts.Count - $mustFail.Count)/$($musts.Count) MUST passed."
if ($mustFail) { $lines += "Failed MUST: " + (($mustFail | ForEach-Object { $_.Id }) -join ", ") + "." }
if ($wantFail) { $lines += "Failed WANT (tracked workarounds): " + (($wantFail | ForEach-Object { $_.Id }) -join ", ") + "." }

$lines -join "`n" | Out-File -FilePath $OutFile -Encoding utf8
Write-Host "`nPart A: $partA (wrote $OutFile)"
if ($mustFail.Count -gt 0) { exit 1 }
exit 0
