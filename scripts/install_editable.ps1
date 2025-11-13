# Powershell helper to install the project in editable mode
# Run this from anywhere; it will cd into the DISCORD BOT folder and run pip install -e .

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
# If script is executed from within the repository, project root should be two levels up
$discordBotDir = Join-Path $projectDir '..'
$discordBotDir = Resolve-Path -Path $discordBotDir

Write-Host "Installing project in editable mode from: $discordBotDir"
Set-Location -LiteralPath $discordBotDir
pip install -e .
