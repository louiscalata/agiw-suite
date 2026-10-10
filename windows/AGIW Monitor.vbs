' AGIW Inference Monitor (Windows) - starts the tray shell without a console window.
' Pass /quiet (the sign-in shortcut does) to start without opening the dashboard.
Set shell = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
openArg = " -OpenDashboard"
If WScript.Arguments.Count > 0 Then
  If LCase(WScript.Arguments(0)) = "/quiet" Then openArg = ""
End If
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File """ & here & "\agiw-monitor.ps1""" & openArg, 0, False
