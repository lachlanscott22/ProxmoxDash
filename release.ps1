<#
.SYNOPSIS
  Publish the dashboard to GitHub from Windows (first push and every update after).

.EXAMPLE
  .\release.ps1                         # patch bump (1.0.0 -> 1.0.1): tests, commit, tag, push
  .\release.ps1 minor "Add X"           # minor bump with a message
  .\release.ps1 -Bump 2.3.0 -Message "Msg"
  .\release.ps1 -Bump none "Docs only"  # push without offering an update to nodes
  .\release.ps1 -Yes                    # skip the confirmation prompt

  Double-click friendly wrapper: release.bat
#>
param(
    [Parameter(Position = 0)][string]$Bump = "patch",
    [Parameter(Position = 1)][string]$Message = "",
    [switch]$Yes
)
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot

function Fail($m) { Write-Host $m -ForegroundColor Red; exit 1 }
function G { & git @args; if ($LASTEXITCODE -ne 0) { Fail "git $($args -join ' ') failed." } }
function GOut { $o = & git @args 2>$null; if ($LASTEXITCODE -ne 0) { return $null } ; return ($o -join "`n").Trim() }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "Git isn't installed. Get it from https://git-scm.com/download/win" }
if (-not (Test-Path .git)) { & git init -q -b main; if ($LASTEXITCODE -ne 0) { Fail "git init failed" } }

# --- identity: keep your real name/email out of the public history
$email = GOut config --local user.email
if (-not $email -or $email -notmatch "^[A-Za-z0-9-]+@users\.noreply\.github\.com$") {
    Write-Host "Commits are public. Use a private GitHub identity for this repo."
    $gh = Read-Host "Your GitHub username"
    if ($gh -notmatch "^[A-Za-z0-9-]+$") { Fail "That should be just your GitHub username (letters, numbers, dashes), not an email address." }
    G config --local user.name $gh
    G config --local user.email "$gh@users.noreply.github.com"
    Write-Host "Using $gh@users.noreply.github.com for this repo only."
}

# --- remote
$url = GOut remote get-url origin
if (-not $url) {
    Write-Host "No GitHub repository set yet. Create an EMPTY repo on github.com (no README), then paste its URL."
    $url = Read-Host "Repository URL (https://github.com/<owner>/<repo>.git)"
    if ($url -notmatch "^(https://github\.com/|git@github\.com:)[\w.-]+/[\w.-]+?(\.git)?$") { Fail "That doesn't look like a GitHub repository URL." }
    G remote add origin $url
}
$branch = GOut rev-parse --abbrev-ref HEAD
if ($branch -ne "main") {
    if ($branch -and $branch -ne "HEAD" -and (GOut rev-parse --verify -q HEAD)) { Fail "You're on '$branch'. Releases come from 'main'." }
    G checkout -q -B main
}
& git fetch -q origin 2>$null
if ((GOut rev-parse -q --verify origin/main) -and (GOut rev-list HEAD..origin/main)) {
    Fail "origin/main has commits you don't have. Run 'git pull --rebase' first."
}

# --- tests
$py = $null
foreach ($c in @("python", "py", "python3")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { & $c -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $py = $c; break } }
}
if (-not $py) { Fail "Python 3 not found." }
Write-Host "Running tests..."
& $py -m unittest discover -s tests *> $null
if ($LASTEXITCODE -ne 0) { Fail "Tests failed. Run: $py -m unittest discover -s tests" }

# --- version
$old = (Get-Content VERSION -Raw).Trim()
if ($Bump -in @("patch", "minor", "major")) {
    $p = $old.Split(".") | ForEach-Object { [int]$_ }
    switch ($Bump) {
        "patch" { $p[2]++ }
        "minor" { $p[1]++; $p[2] = 0 }
        "major" { $p[0]++; $p[1] = 0; $p[2] = 0 }
    }
    $new = $p -join "."
} elseif ($Bump -eq "none") { $new = $old }
elseif ($Bump -match "^\d+\.\d+\.\d+$") { $new = $Bump }
else { Fail "Bump must be patch, minor, major, none or X.Y.Z" }
if (-not $Message) { $Message = if ($Bump -eq "none") { "Update" } else { "Release $new" } }
$slug = ($url -replace "^(https?://[^/]+/|git@[^:]+:)", "") -replace "\.git$", ""

Write-Host "Remote : $url"
Write-Host "Version: $old -> $new"
Write-Host "Message: $Message"
& git status --short
if (-not $Yes) {
    $a = Read-Host "Commit all of the above and push to GitHub? [y/N]"
    if ($a -notin @("y", "Y")) { Write-Host "Cancelled."; exit 1 }
}

# --- apply version + repo slug (LF line endings, no BOM)
$utf8 = New-Object System.Text.UTF8Encoding($false)
if ($Bump -ne "none") {
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot "VERSION"), "$new`n", $utf8)
    $f = Join-Path $PSScriptRoot "pve-dash.py"
    $t = [IO.File]::ReadAllText($f)
    $t = [regex]::Replace($t, '(?m)^VERSION = "[0-9.]+"', "VERSION = `"$new`"")
    [IO.File]::WriteAllText($f, $t, $utf8)
}
$f = Join-Path $PSScriptRoot "pve-dash.py"
$t = [IO.File]::ReadAllText($f)
[IO.File]::WriteAllText($f, $t.Replace('or "OWNER/pve-dash"', "or `"$slug`""), $utf8)

# --- privacy guard: refuse to publish anything matching your local block-list
G add -A
if (Test-Path .pushguard.local) {
    $pat = (Get-Content .pushguard.local | Where-Object { $_ -and -not $_.StartsWith("#") }) -join "|"
    if ($pat) {
        # the repo address in the REPO default line is public by nature, so it is allowed
        $hits = & git grep --cached -I -i -n -E $pat 2>$null | Where-Object { $_ -notmatch "PVE_DASH_REPO" }
        if ($hits) {
            & git reset -q
            & git checkout -- VERSION pve-dash.py 2>$null   # undo the version bump so a re-run doesn't bump twice
            Write-Host ($hits | Select-Object -First 10 | Out-String)
            Fail "Blocked: the lines above match .pushguard.local. Remove them and run again."
        }
    }
} else {
    Write-Host "Tip: create .pushguard.local (one regex per line, e.g. your name) to block accidental personal info." -ForegroundColor Yellow
}

& git commit -q -m $Message
if ($LASTEXITCODE -ne 0) { Write-Host "(nothing new to commit)" }
if ($Bump -ne "none") {
    G tag -f "v$new" | Out-Null
    G push -q origin "v$new" --force   # tag first, so the files exist before VERSION advertises them
}
G push -q -u origin main
Write-Host "Done. $slug is now at $new." -ForegroundColor Green
if ($Bump -ne "none") { Write-Host "Nodes will offer the update within 6 hours, or straight away via Settings > Software > Check now." }
