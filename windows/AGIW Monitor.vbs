' AGIW Inference Monitor (Windows) - starts the tray shell without a console window.
' Pass /quiet (the sign-in shortcut does) to start without opening the dashboard.
' PowerShell is started by its full System32 path so nothing earlier on PATH can stand in for it.
Set shell = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
ps = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
openArg = " -OpenDashboard"
If WScript.Arguments.Count > 0 Then
  If LCase(WScript.Arguments(0)) = "/quiet" Then openArg = ""
End If
shell.Run """" & ps & """ -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File """ & here & "\agiw-monitor.ps1""" & openArg, 0, False
