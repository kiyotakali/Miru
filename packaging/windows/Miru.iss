#define MyAppName "Miru"
#define MyAppVersion "0.2.0"
#define MyAppPublisher "Miru"
#define MyAppExeName "Miru.exe"

[Setup]
AppId={{F1D50D2C-1D0F-4A9F-89CB-CC0E68E59107}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Miru
DefaultGroupName=Miru
DisableProgramGroupPage=yes
OutputDir=..\..\dist\windows-installer
OutputBaseFilename=Miru-Windows-x64-Setup
SetupIconFile=..\..\src-tauri\icons\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Files]
Source: "..\..\dist\Miru\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Miru"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Miru"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Miru"; Flags: nowait postinstall skipifsilent
