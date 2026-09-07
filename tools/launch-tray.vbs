' Launch the Azeroth Realm tray with no console window at all.
' WScript.Shell.Run's second argument (0 = SW_HIDE) suppresses the window before it
' paints, which -WindowStyle Hidden alone does not - that still flashes a console.
Option Explicit
Dim sh, hub, cmd
Set sh = CreateObject("WScript.Shell")
' Derive the hub from this script's own location (tools\ is one level down)
' so a clone anywhere works without editing this file.
Dim fso : Set fso = CreateObject("Scripting.FileSystemObject")
hub = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & hub & "\tools\tray.ps1"""
' 0 = hidden window, False = do not wait for it to exit
sh.Run cmd, 0, False
