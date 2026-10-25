; GHSpeed Windows 安装包(Inno Setup)
; 免管理员安装到用户目录;开启加速时应用内自行弹 UAC
; CI 调用: iscc /DAppVersion=x.y.z installer.iss

#define AppName "GHSpeed"
#define AppExeName "GHSpeed.exe"
#ifndef AppVersion
#define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7E1B6C5A-52D4-4A9E-9B3F-8C21A0B45C01}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} v{#AppVersion}
AppPublisher=archerzing-tech
AppPublisherURL=https://github.com/archerzing-tech/ghspeed
AppSupportURL=https://github.com/archerzing-tech/ghspeed
DefaultDirName={userpf}\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
OutputBaseFilename=GHSpeed-windows-setup-v{#AppVersion}
OutputDir=..\..\dist
UninstallDisplayName={#AppName}
ChangesEnvironment=yes

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"
Name: "addpath"; Description: "把命令行 ghspeed 加入 PATH(新开的终端生效)"; GroupDescription: "附加任务:"

[Files]
Source: "..\..\dist\GHSpeed.exe"; DestDir: "{app}"; Flags: ignoreversion
; CLI 单独命名 ghspeed-cli.exe:NTFS 大小写不敏感,GHSpeed.exe/ghspeed.exe 同目录会互相覆盖
Source: "..\..\dist\ghspeed-cli.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即运行 {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  P, AppDir: String;
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addpath') then
  begin
    AppDir := ExpandConstant('{app}');
    P := '';
    RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', P);
    if Pos(';' + Uppercase(AppDir) + ';', ';' + Uppercase(P) + ';') = 0 then
    begin
      if P <> '' then
        P := P + ';';
      P := P + AppDir;
      RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', P);
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  P, AppDir, Item, Removed: String;
  I: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // 从用户 PATH 里摘掉安装目录(手动按 ';' 分割,Pascal Script 无 Split)
    AppDir := ExpandConstant('{app}');
    if RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', P) then
    begin
      Removed := '';
      while Length(P) > 0 do
      begin
        I := Pos(';', P);
        if I = 0 then
        begin
          Item := P;
          P := '';
        end
        else
        begin
          Item := Copy(P, 1, I - 1);
          Delete(P, 1, I);
        end;
        if (Item <> '') and (Uppercase(Item) <> Uppercase(AppDir)) then
        begin
          if Removed <> '' then
            Removed := Removed + ';';
          Removed := Removed + Item;
        end;
      end;
      RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Removed);
    end;
    MsgBox('如之前已开启加速,建议以管理员运行:' + #13#10 +
           '  ' + AppDir + '\ghspeed-cli.exe off' + #13#10 +
           '以还原 hosts 并移除定时任务。', mbInformation, MB_OK);
  end;
end;
