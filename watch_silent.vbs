' watch_silent.vbs - run the intraday watch loop with NO console window.
'
' Why this file exists: watch.bat must be parsed by cmd.exe, and cmd.exe is a
' console-subsystem program, so double-clicking it always opens a black box.
' Because the loop runs until 17:00, that box then stays on screen all day.
' WScript.Shell.Run with window style 0 hides it completely; output still goes
' to watch.log exactly as before.
'
' Usage : double-click this file. To stop, run stop_watch.vbs.
' Details: see SKILL.md, section "Windows: why a console window pops up".

Option Explicit

Dim fso, sh, here, py, args, q, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

q = Chr(34)
here = fso.GetParentFolderName(WScript.ScriptFullName)

py = "C:\Users\ey\AppData\Local\Programs\Python\Python312\python.exe"
If Not fso.FileExists(py) Then py = "python.exe"

args = "--watch --loop --loop-min 10 --until 17:00"
cmd = "cmd /c " & q & py & q & " " & q & here & "\scripts\hk_edge.py" & q & " " _
      & args & " >> " & q & here & "\watch.log" & q & " 2>&1"

sh.CurrentDirectory = here
sh.Run cmd, 0, False
