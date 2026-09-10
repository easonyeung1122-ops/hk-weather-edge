' watch_silent.vbs - start the intraday watch loop with NO console window.
'
' Why this file exists: everything that goes through watch.bat will open a black
' box, because a .bat can only be parsed by cmd.exe and cmd.exe is a
' console-subsystem program. Because the loop runs until 17:00, that box then
' stays on screen all day.
'
' This launcher starts pythonw.exe instead. pythonw.exe is built as a
' GUI-subsystem binary, so Windows never creates a console for it at all and
' there is nothing to show or to hide. Output still goes to watch.log.
'
' Usage : double-click this file. To stop it, run stop_watch.vbs.
' Details: see SKILL.md, section "Windows: why a console window pops up".

Option Explicit

Dim fso, sh, here, pyw, q, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

q = Chr(34)
here = fso.GetParentFolderName(WScript.ScriptFullName)

pyw = "C:\Users\ey\AppData\Local\Programs\Python\Python312\pythonw.exe"
If Not fso.FileExists(pyw) Then pyw = "pythonw.exe"

cmd = q & pyw & q & " " & q & here & "\scripts\watch_silent.py" & q

sh.CurrentDirectory = here
' Window style is irrelevant for pythonw.exe (there is no console to style),
' but True = wait is kept so this process lives exactly as long as the loop.
' watch_silent.py itself refuses to start a second loop if one is already up.
sh.Run cmd, 0, True
