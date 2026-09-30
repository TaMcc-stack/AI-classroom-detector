$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $project 'start.bat'
$desktop = [Environment]::GetFolderPath('Desktop')
# Build the Chinese filename from Unicode code points so Windows PowerShell 5.1
# does not depend on the script file's UTF-8 decoding behavior.
$shortcutName = "$([char]25945)$([char]23460)YOLO$([char]26234)$([char]33021)$([char]25511)$([char]21046).lnk"
$shortcutPath = Join-Path $desktop $shortcutName
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $project
$shortcut.Description = 'Start Classroom YOLO Control'
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,137"
$shortcut.Save()
Write-Host "Desktop shortcut created: $shortcutPath"
